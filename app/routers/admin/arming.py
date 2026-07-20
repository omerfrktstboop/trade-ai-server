"""REAL hesap arming/disarming endpoint'leri (v2 Faz 4).

Gerçek hesapta emir gönderebilmenin TEK yolu buradaki arming akışıdır:

- ``POST /api/admin/arm-real-account`` — gateway'den CANLI hesap okunur;
  hesap REAL değilse veya kimlik alanları eksikse reddedilir.
  ``armedAccountRef``'e gateway'in verdiği sha256 referansı DOĞRUDAN yazılır
  (yeniden hash yok).
- ``POST /api/admin/disarm-real-account`` — koşulsuz disarm (fail-closed yön,
  onay istemez).

Her iki işlem de ``account_events`` + config audit satırları üretir.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.db.session import async_session_factory
from app.models.db import AccountEvent
from app.routers.admin._shared import (
    admin_api_router,
    require_admin,
)
from app.services.admin_config import (
    disarm_real_account,
    get_admin_config_value,
    set_admin_config_value,
)
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    gateway_client,
)

logger = logging.getLogger(__name__)


class ArmRequest(BaseModel):
    reason: str | None = None


class DisarmRequest(BaseModel):
    reason: str | None = None


@admin_api_router.post("/arm-real-account")
async def arm_real_account(request: Request, body: ArmRequest | None = None) -> dict:
    identity = await require_admin(request)

    try:
        account = await gateway_client.get_account()
    except (GatewayUnavailable, GatewayError) as exc:
        raise HTTPException(
            status_code=409, detail=f"gateway account read failed: {exc}"
        ) from exc

    account_ref = str(account.get("accountRef") or "").strip()
    session_ref = str(account.get("accountSessionRef") or "").strip()
    account_type = str(account.get("accountType") or "UNKNOWN").strip().upper()
    account_masked = str(account.get("accountIdMasked") or "").strip()

    if not account_ref:
        raise HTTPException(
            status_code=409,
            detail="gateway did not report accountRef — cannot arm blindly",
        )
    if account_type != "REAL":
        raise HTTPException(
            status_code=409,
            detail=f"active account is {account_type}, not REAL — arming refused "
            "(DEMO account needs no arming)",
        )

    reason = (body.reason if body else None) or "manual arming via admin API"
    async with async_session_factory() as session:
        # armedAccountRef = gateway'in verdiği sha256, olduğu gibi.
        await set_admin_config_value(
            session, "armedAccountRef", account_ref, changed_by=identity, reason=reason
        )
        # Oturum referansı + hesap türünü de kalıcı sakla: oturum değişince
        # watcher otomatik disarm eder (Fix #2).
        await set_admin_config_value(
            session,
            "armedAccountSessionRef",
            session_ref,
            changed_by=identity,
            reason=reason,
        )
        await set_admin_config_value(
            session, "armedAccountType", account_type, changed_by=identity, reason=reason
        )
        await set_admin_config_value(
            session,
            "realAccountArmed",
            "true",
            changed_by=identity,
            reason=reason,
        )
        session.add(
            AccountEvent(
                event_type="ARMED",
                account_ref=account_ref,
                account_session_ref=session_ref or None,
                account_type=account_type,
                source="ADMIN",
                detail=f"armed by {identity}; account={account_masked}",
            )
        )
        await session.commit()

    logger.warning(
        "REAL account ARMED by=%s accountRef=%s account=%s",
        identity,
        account_ref,
        account_masked,
    )
    return {
        "status": "armed",
        "accountRef": account_ref,
        "accountIdMasked": account_masked,
        "accountType": account_type,
    }


@admin_api_router.post("/disarm-real-account")
async def disarm_real_account_endpoint(
    request: Request, body: DisarmRequest | None = None
) -> dict:
    identity = await require_admin(request)
    reason = (body.reason if body else None) or "manual disarm via admin API"

    async with async_session_factory() as session:
        previous_ref = (
            await get_admin_config_value(session, "armedAccountRef")
        ).strip()
        await disarm_real_account(session, reason, changed_by=identity)
        session.add(
            AccountEvent(
                event_type="DISARMED",
                previous_ref=previous_ref or None,
                source="ADMIN",
                detail=f"disarmed by {identity}: {reason}",
            )
        )
        await session.commit()

    logger.warning("REAL account DISARMED by=%s reason=%s", identity, reason)
    return {"status": "disarmed"}
