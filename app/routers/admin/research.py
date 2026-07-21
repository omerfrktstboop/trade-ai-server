"""Admin research/discovery, watchlist, fundamentals, and KAP routes."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import (
    KapEvent,
    ResearchCandidate,
    ResearchCandidateEvent,
    TradeWatchlistSymbol,
    OrderLog,
    RiskDecision,
)
from app.services.admin_config import (
    get_admin_config_value,
)
from app.services.block_reason_classifier import classify_block_reason
from app.services.fundamentals_service import (
    NUMERIC_FIELDS as FUNDAMENTAL_NUMERIC_FIELDS,
    delete_fundamental,
    list_fundamentals,
    upsert_fundamental,
)

from app.services.research_pipeline import (
    promote_research_candidate,
    reject_research_candidate,
    remove_from_trade_watchlist,
)

from app.routers.admin._shared import (
    admin_router,
    admin_api_router,
    templates,
    logger,
    require_admin,
    _to_float,
    _split_csv_symbols,
    _status_strip_context,
    _latest,
    _notify_gateway_config_reload,
)


class FundamentalBody(BaseModel):
    period: str
    fcf_growth_pct: float | None = Field(None, alias="fcfGrowthPct")
    debt_to_equity: float | None = Field(None, alias="debtToEquity")
    net_margin_pct: float | None = Field(None, alias="netMarginPct")
    net_margin_change_pt: float | None = Field(None, alias="netMarginChangePt")
    revenue_growth_pct: float | None = Field(None, alias="revenueGrowthPct")
    notes: str | None = None

    model_config = {"populate_by_name": True}


@admin_router.get("/why-blocked", response_class=HTMLResponse)
async def admin_why_blocked(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    symbol = str(request.query_params.get("symbol") or "").upper()
    category = str(request.query_params.get("category") or "").upper()
    action = str(request.query_params.get("action") or "").upper()
    only_blocked = request.query_params.get("only_blocked") == "1"
    rows: list[dict[str, Any]] = []
    try:
        async with async_session_factory() as session:
            risks = await _latest(session, RiskDecision, 250)
            orders = await _latest(session, OrderLog, 250)
            status_ctx = await _status_strip_context(session)
        for row in risks:
            if only_blocked and row.allow_order:
                continue
            reason = row.reason or ""
            rows.append(
                {
                    "created_at": row.created_at,
                    "request_id": row.request_id,
                    "symbol": row.symbol,
                    "action": row.action,
                    "confidence": row.confidence,
                    "risk_score": row.risk_score,
                    "allow_order": row.allow_order,
                    "order_type": row.order_type,
                    "qty": row.qty,
                    "price": row.entry_max,
                    "reason": reason,
                    "category": classify_block_reason(reason),
                }
            )
        for row in orders:
            if row.status.upper() not in {"REJECTED", "ERROR", "CANCELED"}:
                continue
            reason = row.matrix_message or row.status
            rows.append(
                {
                    "created_at": row.created_at,
                    "request_id": row.request_id,
                    "symbol": row.symbol,
                    "action": row.action,
                    "confidence": None,
                    "risk_score": None,
                    "allow_order": False,
                    "order_type": "LIMIT",
                    "qty": row.qty,
                    "price": row.price,
                    "reason": reason,
                    "category": classify_block_reason(reason),
                }
            )
    except Exception as exc:
        logger.warning("Why blocked query failed: %s", exc)
        status_ctx = {
            "status_mode": "UNKNOWN",
            "status_kill_switch": False,
            "status_profile_code": "UNKNOWN",
            "status_profile_risk_level": "UNKNOWN",
            "status_ai_degraded": None,
        }
    rows = [
        r
        for r in rows
        if (not symbol or r["symbol"] == symbol)
        and (not category or r["category"] == category)
        and (not action or r["action"] == action)
    ]
    rows.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
    categories = Counter(r["category"] for r in rows)
    symbols = Counter(r["symbol"] for r in rows)
    summary = {
        "total": len(rows),
        "category": categories.most_common(1)[0][0] if categories else "-",
        "symbol": symbols.most_common(1)[0][0] if symbols else "-",
        "confidence_low": categories.get("CONFIDENCE_LOW", 0),
    }
    return templates.TemplateResponse(
        request,
        "admin/why_blocked.html",
        {
            "identity": identity,
            "active": "why-blocked",
            "rows": rows,
            "summary": summary,
            "filters": {
                "symbol": symbol,
                "category": category,
                "action": action,
                "only_blocked": only_blocked,
            },
            **status_ctx,
        },
    )


@admin_router.get("/watchlist", response_class=HTMLResponse)
async def admin_watchlist(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(TradeWatchlistSymbol, ResearchCandidate).outerjoin(
                        ResearchCandidate,
                        ResearchCandidate.symbol == TradeWatchlistSymbol.symbol,
                    )
                )
            ).all()
        )
    return templates.TemplateResponse(
        request,
        "admin/watchlist.html",
        {"identity": identity, "active": "watchlist", "rows": rows},
    )


@admin_router.post("/research/{symbol}/promote")
async def admin_research_promote(request: Request, symbol: str) -> RedirectResponse:
    """Kontrollü/manuel terfi: research bulgusu ne olursa olsun (AI'nin 2-pass
    onayını beklemeden de) admin bir sembolü Trade Watchlist'e sokabilir —
    kontrol tamamen admin'de, AI skoru sadece karar destek bilgisidir."""
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "").strip() or (
        f"Manual promotion from Research page by {identity}"
    )
    async with async_session_factory() as session:
        await promote_research_candidate(
            session, symbol, reason=reason, changed_by=identity
        )
    await _notify_gateway_config_reload()
    filter_qs = str(request.query_params.get("filter") or "")
    redirect_url = "/admin/research" + (f"?filter={filter_qs}" if filter_qs else "")
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/research/{symbol}/reject")
async def admin_research_reject(request: Request, symbol: str) -> RedirectResponse:
    """"Şimdilik değil" — kalıcı kara liste değildir (declineSymbols'e
    dokunmaz); discovery ileride yeniden tespit ederse aday geri dönebilir."""
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "").strip() or (
        f"Manually rejected from Research page by {identity}"
    )
    async with async_session_factory() as session:
        await reject_research_candidate(
            session, symbol, reason=reason, changed_by=identity
        )
    filter_qs = str(request.query_params.get("filter") or "")
    redirect_url = "/admin/research" + (f"?filter={filter_qs}" if filter_qs else "")
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/watchlist/{symbol}/remove")
async def admin_watchlist_remove(request: Request, symbol: str) -> RedirectResponse:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "").strip() or (
        f"Manually removed from Watchlist page by {identity}"
    )
    async with async_session_factory() as session:
        await remove_from_trade_watchlist(session, symbol, reason=reason)
    return RedirectResponse("/admin/watchlist", status_code=status.HTTP_303_SEE_OTHER)


RESEARCH_FRESH_WINDOW = timedelta(hours=24)


def _research_rr_ratio(
    entry_max: float | None, stop_loss: float | None, target_price: float | None
) -> float | None:
    """Reward/risk ratio: (target - entry) / (entry - stop).

    This is the "asymmetric opportunity" measure — how many units of upside
    per unit of downside. None when any leg is missing or the stop isn't
    below the entry (degenerate/invalid geometry).
    """
    if entry_max is None or stop_loss is None or target_price is None:
        return None
    risk = entry_max - stop_loss
    if risk <= 0:
        return None
    return (target_price - entry_max) / risk


def _research_sort_key(row: dict[str, Any]) -> tuple:
    """BUYs first (best R/R, then confidence), then WAITs by confidence,
    then SELLs. Rows with an R/R ratio outrank same-action rows without."""
    priority = {"BUY": 0, "WAIT": 1, "SELL": 2}.get(row["action"], 3)
    rr = row["rr"]
    return (
        priority,
        0 if rr is not None else 1,
        -(rr if rr is not None else 0.0),
        -(row["confidence"] or 0.0),
    )


def _research_rank_rows(decisions: list[Any]) -> list[dict[str, Any]]:
    """Turn latest-per-symbol RiskDecision rows into a ranked opportunity
    list. Pure function so the ranking logic is unit-testable."""
    rows: list[dict[str, Any]] = []
    for d in decisions:
        rows.append(
            {
                "symbol": d.symbol,
                "action": d.action,
                "confidence": d.confidence,
                "risk_score": d.risk_score,
                "rr": _research_rr_ratio(d.entry_max, d.stop_loss, d.target_price),
                "entry_min": d.entry_min,
                "entry_max": d.entry_max,
                "stop_loss": d.stop_loss,
                "target_price": d.target_price,
                "reason": d.reason,
                "request_id": d.request_id,
                "created_at": d.created_at,
            }
        )
    rows.sort(key=_research_sort_key)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


@admin_router.get("/research", response_class=HTMLResponse)
async def admin_research(request: Request) -> HTMLResponse:
    """Show discovery candidates, research scores, promotion state and timeline."""
    identity = await require_admin(request)
    selected_filter = str(request.query_params.get("filter") or "all").lower()
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        candidates = (
            (
                await session.execute(
                    select(ResearchCandidate).order_by(
                        ResearchCandidate.last_detected_at.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(ResearchCandidateEvent)
                    .order_by(ResearchCandidateEvent.created_at.desc())
                    .limit(1000)
                )
            )
            .scalars()
            .all()
        )
        active_trade_rows = (
            (
                await session.execute(
                    select(TradeWatchlistSymbol).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            )
            .scalars()
            .all()
        )
        trade_by_symbol = {row.symbol: row for row in active_trade_rows}
        trade_symbols = set(trade_by_symbol)
        status_ctx = await _status_strip_context(session)

    def visible(row: ResearchCandidate) -> bool:
        if selected_filter == "pending":
            return row.status in {"DETECTED", "RESEARCH_PENDING"}
        if selected_filter == "ready":
            return row.status == "READY_FOR_PROMOTION"
        if selected_filter == "near":
            return 60 <= float(row.ai_research_score or 0) < 75
        if selected_filter == "promoted":
            return row.status == "PROMOTED" or row.symbol in trade_symbols
        if selected_filter == "rejected":
            return row.status == "REJECTED"
        if selected_filter == "expired":
            return row.status == "EXPIRED"
        return True

    rows = [row for row in candidates if visible(row)]
    events_by_symbol: dict[str, list[ResearchCandidateEvent]] = {}
    for event in events:
        events_by_symbol.setdefault(event.symbol, []).append(event)

    return templates.TemplateResponse(
        request,
        "admin/research.html",
        {
            "identity": identity,
            "active": "research",
            "rows": rows,
            "events_by_symbol": events_by_symbol,
            "trade_symbols": trade_symbols,
            "trade_by_symbol": trade_by_symbol,
            "selected_filter": selected_filter,
            **status_ctx,
        },
    )


async def _fundamentals_page(
    request: Request, identity: str, *, error: str | None = None
) -> HTMLResponse:
    async with async_session_factory() as session:
        rows = await list_fundamentals(session)
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)

    rows_by_symbol = {row.symbol: row for row in rows}
    # Watchlist symbols first (alphabetical), then any leftover rows for
    # symbols that have since been removed from the watchlist.
    symbols = sorted(_split_csv_symbols(allowed_raw))
    extra_symbols = sorted(set(rows_by_symbol) - set(symbols))

    return templates.TemplateResponse(
        request,
        "admin/fundamentals.html",
        {
            "identity": identity,
            "active": "fundamentals",
            "symbols": symbols,
            "extra_symbols": extra_symbols,
            "rows_by_symbol": rows_by_symbol,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.get("/fundamentals", response_class=HTMLResponse)
async def admin_fundamentals(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _fundamentals_page(request, identity)


@admin_router.get("/kap", response_class=HTMLResponse)
async def admin_kap(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(KapEvent).order_by(KapEvent.cached_at.desc()).limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/kap.html",
        {"identity": identity, "active": "kap", "rows": rows, "risk_only": False},
    )


@admin_router.get("/kap-risk", response_class=HTMLResponse)
async def admin_kap_risk(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(KapEvent)
                    .where(KapEvent.risk_level.in_(("HIGH", "BLOCKING")))
                    .order_by(KapEvent.cached_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/kap.html",
        {"identity": identity, "active": "kap", "rows": rows, "risk_only": True},
    )


@admin_router.post("/fundamentals/{symbol}")
async def admin_fundamentals_upsert(request: Request, symbol: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    numeric = {
        field: _to_float(form.get(field)) for field in FUNDAMENTAL_NUMERIC_FIELDS
    }

    try:
        async with async_session_factory() as session:
            await upsert_fundamental(
                session,
                symbol,
                period=str(form.get("period") or ""),
                changed_by=identity,
                notes=str(form.get("notes") or "").strip() or None,
                **numeric,
            )
    except ValueError as exc:
        return await _fundamentals_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/fundamentals", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/fundamentals/{symbol}/delete")
async def admin_fundamentals_delete(request: Request, symbol: str) -> Any:
    await require_admin(request)
    async with async_session_factory() as session:
        await delete_fundamental(session, symbol)
    return RedirectResponse(
        "/admin/fundamentals", status_code=status.HTTP_303_SEE_OTHER
    )


def _fundamental_dict(row: Any) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "period": row.period,
        "fcfGrowthPct": row.fcf_growth_pct,
        "debtToEquity": row.debt_to_equity,
        "netMarginPct": row.net_margin_pct,
        "netMarginChangePt": row.net_margin_change_pt,
        "revenueGrowthPct": row.revenue_growth_pct,
        "notes": row.notes,
        "updatedBy": row.updated_by,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


@admin_api_router.get("/fundamentals")
async def admin_api_list_fundamentals(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        rows = await list_fundamentals(session)
    return [_fundamental_dict(row) for row in rows]


@admin_api_router.put("/fundamentals/{symbol}")
async def admin_api_upsert_fundamental(
    request: Request, symbol: str, body: FundamentalBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            row = await upsert_fundamental(
                session,
                symbol,
                period=body.period,
                changed_by=identity,
                notes=body.notes,
                fcf_growth_pct=body.fcf_growth_pct,
                debt_to_equity=body.debt_to_equity,
                net_margin_pct=body.net_margin_pct,
                net_margin_change_pt=body.net_margin_change_pt,
                revenue_growth_pct=body.revenue_growth_pct,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _fundamental_dict(row)


@admin_api_router.delete("/fundamentals/{symbol}")
async def admin_api_delete_fundamental(request: Request, symbol: str) -> dict[str, str]:
    await require_admin(request)
    async with async_session_factory() as session:
        existed = await delete_fundamental(session, symbol)
    if not existed:
        raise HTTPException(status_code=404, detail=f"No fundamentals for {symbol}")
    return {"status": "ok", "symbol": symbol.strip().upper()}
