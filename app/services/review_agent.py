"""Review agent — haftalık self-reflection döngüsü (Task 7).

Bu bot her hafta kendi kapanan işlemlerine bakar: hangileri stop-loss'a
takılıp zararla kapandı, ve neden? ``ai_decisions`` + ``order_logs`` +
``risk_decisions`` tablolarından round-trip (BUY→SELL) eşleştirmesi yapar,
stop-loss'ta zararla kapananları LLM'e post-mortem sorusu olarak sorar:
haberi mi yanlış okuduk, AKD/akıllı parayı mı yanlış yorumladık, yoksa stop
mu dardı? Çıkan dersler ``ai_lessons_learned`` tablosuna, insan onayı
bekleyen (``PENDING_REVIEW``) satırlar olarak yazılır — bu servis
``app/core/prompts.py``'ı ASLA kendi kendine değiştirmez.

Round-trip eşleştirmesi basit LIFO'dur: incelenen dönemde kapanan (FILLED
SELL) her pozisyon, aynı sembolün o satıştan önceki en son FILLED BUY'ıyla
eşleştirilir. Bu bot sembol başına tipik olarak tek pozisyon taşıdığından
(RiskEngine'in qty clamp'leri) bu basit eşleşme pratikte doğrudur; çoklu
kısmi giriş/çıkış senaryoları bu ilk sürümün kapsamı dışındadır.

Çalıştırma::

    python -m app.services.review_agent            # önceki takvim haftası
    python -m app.services.review_agent 2026-07-06  # belirli haftanın Pazartesi'si

Haftalık zamanlanmış çalıştırma için işletim sistemi görev zamanlayıcısı
(Windows Task Scheduler / cron) kullanılması önerilir — bu modül kendi
zamanlayıcısını taşımaz.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.core.prompts import get_review_system_prompt
from app.db.session import async_session_factory
from app.models.db import AiDecision, AiLessonLearned, OrderLog, RiskDecision
from app.models.db.ai_lesson_learned import ROOT_CAUSES, STATUS_PENDING
from app.services.ai_provider import (
    AiProvider,
    extract_json_object,
    get_default_provider,
)

logger = logging.getLogger(__name__)


# ── Round-trip eşleştirme ────────────────────────────────────────────────────────


@dataclass
class RoundTripTrade:
    symbol: str
    buy_request_id: str
    buy_price: float
    buy_qty: float
    buy_at: datetime
    sell_request_id: str
    sell_price: float
    sell_qty: float
    sell_at: datetime
    stop_loss: float | None
    target_price: float | None
    entry_reason: str | None
    entry_confidence: float | None
    entry_context: dict[str, Any] | None  # ai_decision.raw_request (news/broker flow)

    @property
    def matched_qty(self) -> float:
        return min(self.buy_qty, self.sell_qty)

    @property
    def realized_pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.matched_qty

    @property
    def realized_pnl_pct(self) -> float | None:
        if self.buy_price <= 0:
            return None
        return (self.sell_price - self.buy_price) / self.buy_price * 100

    @property
    def is_stop_loss_hit(self) -> bool:
        """Exit fiyatı, giriş anındaki stop-loss'a (tolerans payıyla) isabet etti mi?

        Yalnızca stop_loss kayıtlıysa VE realized_pnl negatifse anlamlıdır —
        kârlı bir kapanış "stop'a isabet" sayılmaz, stop zaten yukarı tetiklenmez.
        """
        if self.stop_loss is None or self.stop_loss <= 0:
            return False
        if self.realized_pnl >= 0:
            return False
        tolerance = 1 + settings.review_stop_loss_tolerance_pct / 100
        return self.sell_price <= self.stop_loss * tolerance


async def find_closed_trades(
    session,
    period_start: datetime,
    period_end: datetime,
) -> list[RoundTripTrade]:
    """Verilen dönemde SATIŞ ile kapanmış (FILLED) round-trip işlemleri döndür.

    Eşleşecek BUY bulunamayan SELL'ler (maliyet bilinmiyor, P&L hesaplanamaz)
    sessizce atlanır — veri eksikliği bir hata değil, sadece o trade
    incelemenin kapsamı dışında kalır.
    """
    sells = (
        (
            await session.execute(
                select(OrderLog)
                .where(
                    OrderLog.status == "FILLED",
                    OrderLog.action == "SELL",
                    OrderLog.created_at >= period_start,
                    OrderLog.created_at < period_end,
                )
                .order_by(OrderLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    trades: list[RoundTripTrade] = []
    for sell in sells:
        buy = (
            await session.execute(
                select(OrderLog)
                .where(
                    OrderLog.symbol == sell.symbol,
                    OrderLog.status == "FILLED",
                    OrderLog.action == "BUY",
                    OrderLog.created_at < sell.created_at,
                )
                .order_by(OrderLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if buy is None or not buy.price or buy.price <= 0 or not sell.price:
            continue

        risk = (
            await session.execute(
                select(RiskDecision)
                .where(RiskDecision.request_id == buy.request_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        ai = (
            await session.execute(
                select(AiDecision)
                .where(AiDecision.request_id == buy.request_id)
                .order_by(AiDecision.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        trades.append(
            RoundTripTrade(
                symbol=sell.symbol,
                buy_request_id=buy.request_id,
                buy_price=buy.price,
                buy_qty=buy.qty,
                buy_at=buy.created_at,
                sell_request_id=sell.request_id,
                sell_price=sell.price,
                sell_qty=sell.qty,
                sell_at=sell.created_at,
                stop_loss=risk.stop_loss if risk else None,
                target_price=risk.target_price if risk else None,
                entry_reason=ai.reason if ai else None,
                entry_confidence=ai.confidence if ai else None,
                entry_context=ai.raw_request if ai else None,
            )
        )
    return trades


# ── Dönem hesaplama ──────────────────────────────────────────────────────────────


def previous_week_bounds(reference: date | None = None) -> tuple[datetime, datetime]:
    """Önceki takvim haftasının [Pazartesi 00:00, sonraki Pazartesi 00:00) sınırları.

    ``reference`` verilmezse bugünkü tarih kullanılır (bugünün haftası değil,
    ONDAN ÖNCEKİ tam hafta döner — pazar günü çalıştırıldığında o haftanın
    henüz kapanmamış son gününü değil, tamamlanmış önceki haftayı incelemek
    için).
    """
    tz = ZoneInfo(settings.review_timezone)
    today = reference or datetime.now(tz).date()
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    start = datetime.combine(prev_monday, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(this_monday, datetime.min.time(), tzinfo=tz)
    return start, end


# ── LLM analizi + persist ────────────────────────────────────────────────────────


def build_review_payload(trades: list[RoundTripTrade]) -> list[dict[str, Any]]:
    """Round-trip trade'leri LLM'e gidecek kompakt JSON'a çevir."""
    items: list[dict[str, Any]] = []
    for t in trades:
        entry_ctx = t.entry_context or {}
        item: dict[str, Any] = {
            "symbol": t.symbol,
            "buyAt": t.buy_at.isoformat(),
            "buyPrice": t.buy_price,
            "sellAt": t.sell_at.isoformat(),
            "sellPrice": t.sell_price,
            "qty": t.matched_qty,
            "realizedPnl": round(t.realized_pnl, 2),
            "realizedPnlPct": (
                round(t.realized_pnl_pct, 2) if t.realized_pnl_pct is not None else None
            ),
            "stopLoss": t.stop_loss,
            "targetPrice": t.target_price,
            "entryConfidence": t.entry_confidence,
            "entryReason": t.entry_reason,
        }
        # Giriş anında görülen haber/akıllı-para bağlamı — mevcutsa ekle,
        # payload'ı şişirmemek için sadece ilgili alt-anahtarlar.
        news = entry_ctx.get("newsContext") if isinstance(entry_ctx, dict) else None
        flow = (
            entry_ctx.get("brokerFlowContext") if isinstance(entry_ctx, dict) else None
        )
        if news:
            item["newsContextAtEntry"] = news
        if flow:
            item["brokerFlowContextAtEntry"] = flow
        items.append(item)
    return items


def _validate_lesson(raw: dict[str, Any]) -> dict[str, Any]:
    """LLM'in tek bir lesson objesini güvenli alanlara indirger."""
    root_cause = str(raw.get("rootCause") or "OTHER").strip().upper()
    if root_cause not in ROOT_CAUSES:
        root_cause = "OTHER"
    lesson = str(raw.get("lesson") or "").strip() or "No explanation provided."
    proposed_rule = raw.get("proposedRule")
    proposed_rule = str(proposed_rule).strip() if proposed_rule else None
    symbols = raw.get("affectedSymbols") or []
    if not isinstance(symbols, list):
        symbols = []
    return {
        "root_cause": root_cause,
        "lesson": lesson,
        "proposed_rule": proposed_rule,
        "affected_symbols": [str(s).upper() for s in symbols if s],
    }


async def run_weekly_review(
    reference: date | None = None,
    provider: AiProvider | None = None,
) -> list[AiLessonLearned]:
    """Bir haftalık self-reflection döngüsünü çalıştır.

    Returns:
        Bu çalıştırmada oluşturulan ``AiLessonLearned`` satırları (boş liste
        = ya o hafta stop-loss'ta zararla kapanan trade yok, ya da LLM
        çağrısı/parse başarısız oldu — ikisi de günlüğe INFO/WARNING olarak
        düşer, exception fırlatılmaz).
    """
    period_start, period_end = previous_week_bounds(reference)

    async with async_session_factory() as session:
        trades = await find_closed_trades(session, period_start, period_end)

    flagged = [t for t in trades if t.is_stop_loss_hit]
    if not flagged:
        logger.info(
            "Weekly review: no stop-loss losing trades in period %s..%s (closed_trades=%d)",
            period_start.date(),
            period_end.date(),
            len(trades),
        )
        return []

    logger.info(
        "Weekly review: %d stop-loss losing trade(s) in period %s..%s — asking LLM",
        len(flagged),
        period_start.date(),
        period_end.date(),
    )

    payload = build_review_payload(flagged)
    provider = provider or get_default_provider()
    raw_text = await provider.chat(
        get_review_system_prompt(),
        _dumps(payload),
        max_tokens=1200,
    )

    symbols_involved = ",".join(sorted({t.symbol for t in flagged}))
    total_pnl = round(sum(t.realized_pnl for t in flagged), 2)

    parsed = extract_json_object(raw_text) if raw_text else None
    lessons_raw = parsed.get("lessons") if isinstance(parsed, dict) else None

    if not isinstance(lessons_raw, list) or not lessons_raw:
        logger.warning(
            "Weekly review: LLM response could not be parsed into lessons "
            "(raw_text_len=%d) — persisting a placeholder row for visibility",
            len(raw_text or ""),
        )
        lessons_raw = [
            {
                "rootCause": "OTHER",
                "lesson": "LLM response could not be parsed as JSON lessons.",
                "proposedRule": None,
                "affectedSymbols": list({t.symbol for t in flagged}),
            }
        ]

    created: list[AiLessonLearned] = []
    async with async_session_factory() as session:
        for raw_lesson in lessons_raw:
            if not isinstance(raw_lesson, dict):
                continue
            clean = _validate_lesson(raw_lesson)
            row = AiLessonLearned(
                period_start=period_start,
                period_end=period_end,
                symbols_involved=",".join(clean["affected_symbols"])
                or symbols_involved,
                trades_reviewed_count=len(flagged),
                total_realized_pnl=total_pnl,
                root_cause=clean["root_cause"],
                lesson=clean["lesson"],
                proposed_rule=clean["proposed_rule"],
                raw_llm_response=parsed
                if isinstance(parsed, dict)
                else {"raw_text": raw_text},
                status=STATUS_PENDING,
            )
            session.add(row)
            created.append(row)
        await session.commit()
        for row in created:
            await session.refresh(row)

    logger.info("Weekly review: persisted %d lesson(s)", len(created))
    return created


def _dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


# ── CLI entrypoint ───────────────────────────────────────────────────────────────


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    reference: date | None = None
    if len(sys.argv) > 1:
        reference = date.fromisoformat(sys.argv[1])
    lessons = await run_weekly_review(reference)
    for lesson in lessons:
        print(f"[{lesson.root_cause}] {lesson.lesson}")
        if lesson.proposed_rule:
            print(f"  proposed rule: {lesson.proposed_rule}")


if __name__ == "__main__":
    asyncio.run(_main())
