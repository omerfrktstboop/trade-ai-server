"""Kalıcı, bar-farkında AI çağrı kapısı (Plan Faz 1.2).

``decision_gate.py``'in süreç-içi cache'i token maliyetini düşürür ama
restart'ta sıfırlanır. Bu kapı kalıcıdır: bir ``(sembol, bar, setup parmak
izi)`` üçlüsü için LLM çağrısı yalnızca bir kez talep edilebilir; talep DB'de
saklanır ve restart sonrası aynı bar/setup için çağrı tekrar edilmez.

Sözleşme:
- ``try_claim_ai_call`` çağrıyı atomik olarak talep eder: ilk talep ``True``
  (çağrıyı yap), sonraki talepler ``False`` (atla) döner. Yarış, benzersizlik
  kısıtı + SAVEPOINT ile çözülür (Postgres ve SQLite'ta aynı davranır).
- Bar kimliği çözülemezse (``bar_key is None``) kapı **fail-open**'dır: çağrıya
  izin verilir, tıpkı diğer maliyet kapıları gibi — kalıcı gating bir maliyet
  optimizasyonudur, güvenlik siniri değil.
- Parmak izi setup'ın maddi girdilerini kovalar (bucket'lanmış skor +
  yuvarlanmış seviyeler); küçük gürültü parmak izini değiştirmez, gerçek bir
  setup değişimi değiştirir ve aynı bar içinde yeni çağrıya izin verir.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AiCallClaim
from app.models.signal import SignalRequest

logger = logging.getLogger(__name__)


def resolve_bar_key(request: SignalRequest) -> str | None:
    """Min5 barının kararlı kimliğini üret; çözülemezse None (fail-open).

    Tercih: ``bar_event_utc`` gösterge periyodu sınırına yuvarlanır — böylece
    aynı bar içindeki tüm gözlemler aynı anahtarı paylaşır. Zaman yoksa
    ``actual_bar_period`` + ``bar_data_index`` yedeğine düşülür.
    """
    period = request.indicator_period_seconds or request.actual_bar_period_seconds
    if request.bar_event_utc is not None and period and period > 0:
        epoch = int(request.bar_event_utc.timestamp())
        floored = epoch - (epoch % period)
        return str(floored)
    if request.actual_bar_period and request.bar_data_index is not None:
        return f"{request.actual_bar_period}:{request.bar_data_index}"
    return None


def fingerprint(values: dict[str, Any]) -> str:
    """Değerlerin kararlı kısa hash'i (anahtar sırası ve tip bağımsız)."""
    payload = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_setup_fingerprint(
    *,
    action: str | None,
    setup_score: float | None,
    entry: Any = None,
    stop_loss: Any = None,
    target: Any = None,
    score_bucket: float = 5.0,
    price_decimals: int = 2,
) -> str:
    """Setup'ın maddi girdilerinden parmak izi.

    Skor ``score_bucket`` genişliğinde bucket'lanır ve seviyeler yuvarlanır;
    amaç, önemsiz dalgalanmaların (aynı bar içinde) yeni bir LLM çağrısını
    tetiklememesi, gerçek bir setup değişiminin ise tetiklemesidir.
    """
    bucketed = None
    if setup_score is not None and score_bucket > 0:
        bucketed = int(setup_score // score_bucket)

    def _round(value: Any) -> float | None:
        if value is None:
            return None
        return round(float(value), price_decimals)

    return fingerprint(
        {
            "action": (action or "").upper(),
            "scoreBucket": bucketed,
            "entry": _round(entry),
            "stop": _round(stop_loss),
            "target": _round(target),
        }
    )


async def try_claim_ai_call(
    session: AsyncSession,
    *,
    symbol: str,
    bar_key: str | None,
    setup_fingerprint: str,
    evaluation_purpose: str | None = None,
) -> bool:
    """LLM çağrısını atomik olarak talep et.

    Returns:
        ``True`` çağrıyı yap (bu üçlü ilk kez talep edildi ya da bar kimliği
        çözülemedi → fail-open); ``False`` atla (aynı sembol/bar/setup için
        çağrı zaten talep edilmiş).

    Kayıt eklenirse çağıranın commit'iyle kalıcı olur; çakışma SAVEPOINT
    içinde yakalanıp geri alınır, dış transaction bozulmaz.
    """
    if bar_key is None:
        # Bar kimliği yoksa kalıcı gating uygulanamaz — maliyet kapısı
        # fail-open, çağrıya izin ver.
        return True

    try:
        async with session.begin_nested():
            session.add(
                AiCallClaim(
                    symbol=symbol.upper(),
                    bar_key=bar_key,
                    setup_fingerprint=setup_fingerprint,
                    evaluation_purpose=evaluation_purpose,
                    created_at=datetime.now(UTC),
                )
            )
        return True
    except IntegrityError:
        logger.debug(
            "AI_CALL_ALREADY_CLAIMED symbol=%s barKey=%s fingerprint=%s",
            symbol,
            bar_key,
            setup_fingerprint,
        )
        return False
