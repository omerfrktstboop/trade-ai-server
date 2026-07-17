"""Shared router objects, templates, auth, and cross-cutting helpers for the admin package."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.config import settings
from app.models.db import (
    TradeProfile,
)
from app.services.admin_config import (
    AdminConfigItem,
    list_admin_configs,
)
from app.services.ai_provider import get_ai_provider_status
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    gateway_client,
)
from app.services.trade_profile import (
    get_active_profile,
)

admin_router = APIRouter(tags=["Admin"])

admin_api_router = APIRouter(tags=["Admin API"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent.parent / "templates")
)

_DISPLAY_TZ = ZoneInfo("Europe/Istanbul")


async def _notify_gateway_config_reload() -> None:
    """Best-effort push; gateway also polls every 60 seconds."""
    try:
        response = await gateway_client.reload_config()
        logger.info(
            "Gateway config reload notified profile=%s ok=%s symbols=%s",
            response.get("profileCode"),
            response.get("ok"),
            ",".join(response.get("symbols") or []),
        )
    except (GatewayUnavailable, GatewayError) as exc:
        logger.warning("Gateway config reload notification failed: %s", exc)


def _local_time(value: datetime | None) -> str:
    """Render a DB timestamp (stored as UTC via func.now()) in Europe/Istanbul
    local time for the admin panel — DB values stay UTC, only the display
    layer converts, so sorting/comparisons elsewhere are unaffected."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["local_time"] = _local_time

logger = logging.getLogger(__name__)

ADMIN_COOKIE_NAME = "trade_ai_admin"

ADMIN_COOKIE_TTL_SECONDS = 8 * 60 * 60


def _split_csv_symbols(raw: str) -> set[str]:
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


async def _admin_identity(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if hmac.compare_digest(token, settings.effective_admin_api_token):
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


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _config_lookup(session: Any) -> dict[str, AdminConfigItem]:
    return {item.key: item for item in await list_admin_configs(session)}


async def _status_strip_context(
    session: Any,
    *,
    configs: dict[str, AdminConfigItem] | None = None,
    profile: TradeProfile | None = None,
) -> dict[str, Any]:
    """Trading mode / kill switch / active trade profile — shown in the
    header on every admin page so the current risk posture is always
    visible without needing to visit Dashboard or Trade Profiles first."""
    configs = configs if configs is not None else await _config_lookup(session)
    profile = profile if profile is not None else await get_active_profile(session)
    try:
        ai_status = get_ai_provider_status()
    except Exception:
        ai_status = {"isDegraded": None}
    return {
        "status_mode": configs["systemMode"].value,
        "status_kill_switch": configs["killSwitchEnabled"].value == "true",
        "status_profile_code": profile.code,
        "status_profile_risk_level": profile.risk_level,
        "status_ai_degraded": ai_status["isDegraded"],
    }


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
    secret = f"{settings.admin_password}:{settings.effective_admin_api_token}".encode(
        "utf-8"
    )
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
