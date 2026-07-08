"""Admin panel and admin API routes."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import (
    AiDecision,
    BotPosition,
    ConfigAuditLog,
    LockedPosition,
    OrderLog,
    RiskDecision,
)
from app.services.admin_config import (
    AdminConfigItem,
    RISKY_CONFIRMATION,
    get_admin_config_value,
    list_admin_configs,
    set_admin_config_value,
)
from app.services.daily_trade_count import get_today_trade_counts

admin_router = APIRouter(tags=["Admin"])
admin_api_router = APIRouter(tags=["Admin API"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

ADMIN_COOKIE_NAME = "trade_ai_admin"
ADMIN_COOKIE_TTL_SECONDS = 8 * 60 * 60


def _split_csv_symbols(raw: str) -> set[str]:
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


class AdminConfigUpdate(BaseModel):
    value: Any
    reason: str | None = None
    confirmation: str | None = None


class EmergencyAction(BaseModel):
    reason: str | None = None
    confirmation: str | None = None


async def _admin_identity(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if hmac.compare_digest(token, settings.api_token):
            return "api-token"

    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie and _verify_admin_cookie(cookie):
        return "admin"

    return None


async def require_admin(request: Request) -> str:
    identity = await _admin_identity(request)
    if identity:
        return identity
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


@admin_router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"error": None},
    )


@admin_router.post("/login")
async def admin_login(request: Request) -> Any:
    form = await request.form()
    password = str(form.get("password") or "")
    if not hmac.compare_digest(password, settings.admin_password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Invalid admin password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        _make_admin_cookie(),
        max_age=ADMIN_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )
    return response


@admin_router.post("/logout")
async def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        today_counts = await get_today_trade_counts(session, "*")
        latest_risk = await _latest(session, RiskDecision, 20)
        latest_orders = await _latest(session, OrderLog, 20)

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "identity": identity,
            "active": "dashboard",
            "configs": configs,
            "today_trade_count": today_counts.bot_count,
            "latest_risk": latest_risk,
            "latest_orders": latest_orders,
        },
    )


@admin_router.get("/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await list_admin_configs(session)

    return templates.TemplateResponse(
        request,
        "admin/config.html",
        {
            "identity": identity,
            "active": "config",
            "configs": configs,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            "form_values": {},
            "reason": "",
        },
    )


@admin_router.post("/config", response_class=HTMLResponse)
async def admin_config_update(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Admin panel update")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            configs = await list_admin_configs(session)
            for item in configs:
                if item.key not in form:
                    continue
                await set_admin_config_value(
                    session,
                    item.key,
                    form[item.key],
                    changed_by=identity,
                    reason=reason,
                    confirmation=confirmation,
                )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await list_admin_configs(session)
        return templates.TemplateResponse(
            request,
            "admin/config.html",
            {
                "identity": identity,
                "active": "config",
                "configs": configs,
                "confirmation": RISKY_CONFIRMATION,
                "error": str(exc),
                "message": None,
                "form_values": form,
                "reason": reason,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse("/admin/config", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.get("/positions", response_class=HTMLResponse)
async def admin_positions(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        bot_positions = await _latest(session, BotPosition, 100, order_field="updated_at")
        locked_positions = await _latest(
            session, LockedPosition, 100, order_field="created_at"
        )
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")

    allowed_symbols = _split_csv_symbols(allowed_raw)

    return templates.TemplateResponse(
        request,
        "admin/positions.html",
        {
            "identity": identity,
            "active": "positions",
            "bot_positions": bot_positions,
            "locked_positions": locked_positions,
            "allowed_symbols": allowed_symbols,
        },
    )


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

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        ai_decisions = await _latest(session, AiDecision, 20)
        risk_decisions = await _latest(session, RiskDecision, 20)
        order_logs = await _latest(session, OrderLog, 20)
        audit_logs = await _latest(session, ConfigAuditLog, 20)

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
        },
    )


@admin_router.get("/emergency", response_class=HTMLResponse)
async def admin_emergency(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)

    kill_switch = configs["killSwitchEnabled"].value == "true"
    current_mode = configs["tradingMode"].value

    return templates.TemplateResponse(
        request,
        "admin/emergency.html",
        {
            "identity": identity,
            "active": "emergency",
            "configs": configs,
            "kill_switch": kill_switch,
            "current_mode": current_mode,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            "submitted_reason": "",
        },
    )


@admin_router.post("/emergency/{action}")
async def admin_emergency_action(
    request: Request,
    action: str,
) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or f"Emergency action: {action}")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            await _apply_emergency_action(
                session,
                action,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
        kill_switch = configs["killSwitchEnabled"].value == "true"
        current_mode = configs["tradingMode"].value
        return templates.TemplateResponse(
            request,
            "admin/emergency.html",
            {
                "identity": identity,
                "active": "emergency",
                "configs": configs,
                "kill_switch": kill_switch,
                "current_mode": current_mode,
                "confirmation": RISKY_CONFIRMATION,
                "error": str(exc),
                "message": None,
                "submitted_reason": reason,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse("/admin/emergency", status_code=status.HTTP_303_SEE_OTHER)


@admin_api_router.get("/dashboard")
async def admin_api_dashboard(request: Request) -> dict[str, Any]:
    await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        today_counts = await get_today_trade_counts(session, "*")
        latest_risk = await _latest(session, RiskDecision, 20)
        latest_orders = await _latest(session, OrderLog, 20)

    return {
        "tradingMode": configs["tradingMode"].value,
        "killSwitchEnabled": configs["killSwitchEnabled"].value == "true",
        "todayTradeCount": today_counts.bot_count,
        "latestRiskDecisions": [_row_dict(row) for row in latest_risk],
        "latestOrderLogs": [_row_dict(row) for row in latest_orders],
    }


@admin_api_router.get("/config")
async def admin_api_config(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        configs = await list_admin_configs(session)
    return [_config_dict(item) for item in configs if not item.is_sensitive]


@admin_api_router.put("/config/{key}")
async def admin_api_update_config(
    request: Request,
    key: str,
    body: AdminConfigUpdate,
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            item = await set_admin_config_value(
                session,
                key,
                body.value,
                changed_by=identity,
                reason=body.reason,
                confirmation=body.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _config_dict(item)


@admin_api_router.post("/emergency/{action}")
async def admin_api_emergency(
    request: Request,
    action: str,
    body: EmergencyAction | None = None,
) -> dict[str, str]:
    identity = await require_admin(request)
    payload = body or EmergencyAction()
    try:
        async with async_session_factory() as session:
            await _apply_emergency_action(
                session,
                action,
                changed_by=identity,
                reason=payload.reason or f"Emergency action: {action}",
                confirmation=payload.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "action": action}


async def _config_lookup(session: Any) -> dict[str, AdminConfigItem]:
    return {item.key: item for item in await list_admin_configs(session)}


async def _apply_emergency_action(
    session: Any,
    action: str,
    *,
    changed_by: str,
    reason: str,
    confirmation: str | None,
) -> None:
    if action == "force-paper":
        await set_admin_config_value(
            session,
            "tradingMode",
            "PAPER",
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    if action == "enable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            True,
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    if action == "disable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            False,
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    raise ValueError(f"Unsupported emergency action: {action}")


async def _latest(
    session: Any,
    model: Any,
    limit: int,
    *,
    order_field: str = "created_at",
) -> list[Any]:
    column = getattr(model, order_field)
    stmt = select(model).order_by(column.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _config_dict(item: AdminConfigItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "value": item.display_value,
        "valueType": item.value_type,
        "description": item.description,
        "isSensitive": item.is_sensitive,
        "source": item.source,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }


def _row_dict(row: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        result[column.name] = value
    return result


def _make_admin_cookie() -> str:
    expires = int(time.time()) + ADMIN_COOKIE_TTL_SECONDS
    payload = f"admin|{expires}"
    signature = _sign(payload)
    return f"{payload}|{signature}"


def _verify_admin_cookie(cookie: str) -> bool:
    parts = cookie.split("|")
    if len(parts) != 3:
        return False
    user, expires_raw, signature = parts
    if user != "admin":
        return False
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    payload = f"{user}|{expires}"
    return hmac.compare_digest(signature, _sign(payload))


def _sign(payload: str) -> str:
    secret = f"{settings.admin_password}:{settings.api_token}".encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
