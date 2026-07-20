"""Admin replay and order/AI/audit log routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.db import (
    AiDecision,
    ConfigAuditLog,
    MarketSnapshot,
    OrderLog,
    RiskDecision,
    TradeProfile,
)
from app.services.replay import replay_batch
from app.services.trade_profile import (
    list_profiles,
)

from app.routers.admin._shared import (
    admin_router,
    templates,
    logger,
    require_admin,
    _status_strip_context,
    _latest,
)

@admin_router.get("/replay", response_class=HTMLResponse)
async def admin_replay(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    profiles, status_ctx, error = await _replay_page_context()
    return templates.TemplateResponse(
        request,
        "admin/replay.html",
        {
            "identity": identity,
            "active": "replay",
            "profiles": profiles,
            "result": None,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.post("/replay/run", response_class=HTMLResponse)
async def admin_replay_run(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    form = await request.form()
    profile_code = str(form.get("profile_code") or "") or None
    mode = str(form.get("mode") or "PAPER")
    symbols = [
        s.strip().upper()
        for s in str(form.get("symbols") or "").split(",")
        if s.strip()
    ]
    limit = min(200, max(1, int(form.get("limit") or 100)))
    try:
        result = await replay_batch(
            profile_code=profile_code, symbols=symbols or None, limit=limit, mode=mode
        )
        error = None
    except Exception as exc:
        logger.exception("Replay run failed")
        result = None
        error = f"Replay unavailable: {exc}"
    profiles, status_ctx, context_error = await _replay_page_context()
    return templates.TemplateResponse(
        request,
        "admin/replay.html",
        {
            "identity": identity,
            "active": "replay",
            "profiles": profiles,
            "result": result,
            "error": error or context_error,
            **status_ctx,
        },
    )


async def _replay_page_context() -> tuple[
    list[TradeProfile], dict[str, Any], str | None
]:
    """Keep replay diagnostics available even when the database is transiently down."""
    try:
        async with async_session_factory() as session:
            profiles = await list_profiles(session)
            status_ctx = await _status_strip_context(session)
        return profiles, status_ctx, None
    except Exception as exc:
        logger.warning("Replay page DB query failed: %s", exc)
        return (
            [],
            {
                "status_mode": "UNKNOWN",
                "status_kill_switch": False,
                "status_profile_code": "UNKNOWN",
                "status_profile_risk_level": "UNKNOWN",
            },
            f"Database unavailable: {exc}",
        )


# Log tables deletable from /admin/logs, keyed by URL slug.
LOG_TABLES: dict[str, Any] = {
    "ai-decisions": AiDecision,
    "risk-decisions": RiskDecision,
    "order-logs": OrderLog,
    "audit-logs": ConfigAuditLog,
}


async def _logs_page(
    request: Request, identity: str, *, error: str | None = None
) -> HTMLResponse:
    async with async_session_factory() as session:
        ai_decisions = await _latest(session, AiDecision, 20)
        risk_decisions = await _latest(session, RiskDecision, 20)
        order_logs = await _latest(session, OrderLog, 20)
        audit_logs = await _latest(session, ConfigAuditLog, 20)
        status_ctx = await _status_strip_context(session)

    return templates.TemplateResponse(
        request,
        "admin/logs.html",
        {
            "identity": identity,
            "active": "logs",
            "ai_decisions": ai_decisions,
            "risk_decisions": risk_decisions,
            "order_logs": order_logs,
            "audit_logs": audit_logs,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _logs_page(request, identity)


def _log_table_or_404(table: str) -> Any:
    model = LOG_TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Unknown log table: {table}")
    return model


@admin_router.post("/logs/{table}/delete-all")
async def admin_logs_delete_all(request: Request, table: str) -> Any:
    identity = await require_admin(request)
    model = _log_table_or_404(table)
    form = await request.form()
    reason = str(form.get("reason") or "Delete all logs")

    async with async_session_factory() as session:
        result = await session.execute(delete(model))
        await session.commit()

    logger.warning(
        "Admin %s deleted ALL %d rows from %s (reason: %s)",
        identity,
        result.rowcount or 0,
        table,
        reason,
    )
    return RedirectResponse("/admin/logs", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/logs/{table}/delete-selected")
async def admin_logs_delete_selected(request: Request, table: str) -> Any:
    identity = await require_admin(request)
    model = _log_table_or_404(table)
    form = await request.form()
    reason = str(form.get("reason") or "Delete selected logs")
    ids = [int(raw) for raw in form.getlist("ids") if str(raw).strip().isdigit()]

    if not ids:
        return await _logs_page(request, identity, error="Silinecek kayıt seçilmedi")

    async with async_session_factory() as session:
        result = await session.execute(delete(model).where(model.id.in_(ids)))
        await session.commit()

    logger.warning(
        "Admin %s deleted %d selected rows from %s (reason: %s)",
        identity,
        result.rowcount or 0,
        table,
        reason,
    )
    return RedirectResponse("/admin/logs", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.get("/logs/{request_id}", response_class=HTMLResponse)
async def admin_log_detail(request: Request, request_id: str) -> HTMLResponse:
    """Everything recorded for one evaluation: exact payload sent to the AI,
    its raw response, the risk-engine's final decision, and any matching
    order result — all joined by requestId."""
    identity = await require_admin(request)
    async with async_session_factory() as session:
        snapshot = (
            await session.execute(
                select(MarketSnapshot).where(MarketSnapshot.request_id == request_id)
            )
        ).scalar_one_or_none()
        ai_decision = (
            await session.execute(
                select(AiDecision).where(AiDecision.request_id == request_id)
            )
        ).scalar_one_or_none()
        risk_decision = (
            await session.execute(
                select(RiskDecision).where(RiskDecision.request_id == request_id)
            )
        ).scalar_one_or_none()
        order_logs = (
            (
                await session.execute(
                    select(OrderLog)
                    .where(OrderLog.request_id == request_id)
                    .order_by(OrderLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        status_ctx = await _status_strip_context(session)

    def _pretty(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)

    return templates.TemplateResponse(
        request,
        "admin/log_detail.html",
        {
            "identity": identity,
            "active": "logs",
            "request_id": request_id,
            "snapshot": snapshot,
            "ai_decision": ai_decision,
            "risk_decision": risk_decision,
            "order_logs": order_logs,
            "raw_request_json": _pretty(ai_decision.raw_request)
            if ai_decision
            else None,
            "raw_response_json": _pretty(ai_decision.raw_response)
            if ai_decision
            else None,
            **status_ctx,
        },
    )


# Emir yetkisi yalnızca systemMode=AUTO_TRADE + account watcher + risk
# kapılarıyla verilir.
