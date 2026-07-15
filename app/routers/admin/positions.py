"""Admin bot-position routes (view, force-override, force-sell, watchlist add)."""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import (
    BotPosition,
    LockedPosition,
    PositionManagementDecision,
)
from app.services.admin_config import (
    RISKY_CONFIRMATION,
    get_admin_config_value,
    set_admin_config_value,
)
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    gateway_client,
)
from app.services.signal_override import SELL_ALL_SENTINEL_QTY, create_override
from app.services.research_pipeline import add_manual_trade_symbol

from app.routers.admin._shared import (
    admin_router,
    templates,
    require_admin,
    _to_float,
    _split_csv_symbols,
    _status_strip_context,
    _latest,
    _notify_gateway_config_reload,
)


@admin_router.get("/positions/management", response_class=HTMLResponse)
async def admin_position_management(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(PositionManagementDecision)
                    .order_by(PositionManagementDecision.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/position_management.html",
        {"identity": identity, "active": "positions", "rows": rows},
    )


async def _gateway_positions_context() -> dict[str, Any]:
    try:
        payload = await gateway_client.get_positions()
        positions = payload.get("positions")
        if not isinstance(positions, list):
            positions = []
        return {
            "gateway_positions": positions,
            "gateway_positions_meta": {
                "positions_loaded": payload.get("positionsLoaded"),
                "snapshot_complete": payload.get("snapshotCompleteFlag"),
                "snapshot_non_empty": payload.get("snapshotNonEmpty"),
                "snapshot_age_seconds": payload.get("snapshotAgeSeconds"),
                "snapshot_generation": payload.get("snapshotGeneration"),
                "confidence": payload.get("confidence"),
            },
            "gateway_positions_error": None,
        }
    except (GatewayUnavailable, GatewayError, ValueError, TypeError) as exc:
        return {
            "gateway_positions": [],
            "gateway_positions_meta": {},
            "gateway_positions_error": str(exc),
        }


@admin_router.get("/positions", response_class=HTMLResponse)
async def admin_positions(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        bot_positions = await _latest(
            session, BotPosition, 100, order_field="updated_at"
        )
        locked_positions = await _latest(
            session, LockedPosition, 100, order_field="created_at"
        )
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)

    allowed_symbols = _split_csv_symbols(allowed_raw)
    gateway_ctx = await _gateway_positions_context()

    return templates.TemplateResponse(
        request,
        "admin/positions.html",
        {
            "identity": identity,
            "active": "positions",
            "bot_positions": bot_positions,
            "locked_positions": locked_positions,
            "allowed_symbols": allowed_symbols,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            **gateway_ctx,
            **status_ctx,
        },
    )


async def _positions_page_error(
    request: Request, identity: str, error: str
) -> HTMLResponse:
    async with async_session_factory() as session:
        bot_positions = await _latest(
            session, BotPosition, 100, order_field="updated_at"
        )
        locked_positions = await _latest(
            session, LockedPosition, 100, order_field="created_at"
        )
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)
    gateway_ctx = await _gateway_positions_context()

    return templates.TemplateResponse(
        request,
        "admin/positions.html",
        {
            "identity": identity,
            "active": "positions",
            "bot_positions": bot_positions,
            "locked_positions": locked_positions,
            "allowed_symbols": _split_csv_symbols(allowed_raw),
            "confirmation": RISKY_CONFIRMATION,
            "error": error,
            "message": None,
            **gateway_ctx,
            **status_ctx,
        },
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@admin_router.post("/positions/{symbol}/force-override")
async def admin_force_override(request: Request, symbol: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    action = str(form.get("action") or "").strip().upper()
    reason = str(form.get("reason") or "Manual test override")
    confirmation = str(form.get("confirmation") or "")

    if confirmation != RISKY_CONFIRMATION:
        return await _positions_page_error(
            request,
            identity,
            f"force-override requires confirmation={RISKY_CONFIRMATION}",
        )
    if action not in ("BUY", "SELL"):
        return await _positions_page_error(
            request, identity, f"Unsupported override action: {action or '(empty)'}"
        )

    qty = SELL_ALL_SENTINEL_QTY if action == "SELL" else _to_float(form.get("qty"))
    async with async_session_factory() as session:
        await create_override(
            session,
            symbol,
            action,
            qty,
            reason=reason,
            created_by=identity,
            entry_min=_to_float(form.get("entryMin")),
            entry_max=_to_float(form.get("entryMax")),
            stop_loss=_to_float(form.get("stopLoss")),
            target_price=_to_float(form.get("targetPrice")),
        )

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/positions/force-sell-all")
async def admin_force_sell_all(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Force-sell-all test")
    confirmation = str(form.get("confirmation") or "")

    if confirmation != RISKY_CONFIRMATION:
        return await _positions_page_error(
            request,
            identity,
            f"force-sell-all requires confirmation={RISKY_CONFIRMATION}",
        )

    async with async_session_factory() as session:
        positions = await _latest(session, BotPosition, 500, order_field="updated_at")
        symbols = [p.symbol for p in positions if p.qty and p.qty != 0]
        for symbol in symbols:
            await create_override(
                session,
                symbol,
                "SELL",
                SELL_ALL_SENTINEL_QTY,
                reason=reason,
                created_by=identity,
            )

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/positions/add-to-watchlist")
async def admin_add_to_watchlist(request: Request) -> RedirectResponse:
    identity = await require_admin(request)
    form = await request.form()
    symbol = str(form.get("symbol") or "").strip().upper()

    if symbol:
        async with async_session_factory() as session:
            current = _split_csv_symbols(
                await get_admin_config_value(session, "allowedSymbols")
            )
            current.add(symbol)
            await set_admin_config_value(
                session,
                "allowedSymbols",
                ",".join(sorted(current)),
                changed_by=identity,
                reason=f"Added {symbol} to watchlist from Positions page",
            )
            await add_manual_trade_symbol(
                session,
                symbol,
                reason=f"Explicit admin override by {identity} from Positions page",
            )
            await session.commit()

        await _notify_gateway_config_reload()

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)
