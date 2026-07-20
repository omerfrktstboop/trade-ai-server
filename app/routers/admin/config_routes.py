"""Admin runtime config and emergency-action routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.db.session import async_session_factory
from app.services.admin_config import (
    RISKY_CONFIG_KEYS,
    build_admin_config_sections,
    list_admin_configs,
    public_config_keys,
    set_admin_config_value,
    set_admin_config_values,
)
from app.services.notifications import notification_service

from app.routers.admin._shared import (
    admin_router,
    admin_api_router,
    templates,
    require_admin,
    _config_lookup,
    _status_strip_context,
    _config_dict,
    _notify_gateway_config_reload,
)


class AdminConfigUpdate(BaseModel):
    value: Any
    reason: str | None = None


class AdminConfigBatchUpdate(BaseModel):
    values: dict[str, Any]
    reason: str | None = None


class EmergencyAction(BaseModel):
    reason: str | None = None


@admin_router.get("/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        config_sections = build_admin_config_sections(configs.values())
        status_ctx = await _status_strip_context(session, configs=configs)

    return templates.TemplateResponse(
        request,
        "admin/config.html",
        {
            "identity": identity,
            "active": "config",
            "configs": configs,
            "config_sections": config_sections,
            "error": None,
            "message": None,
            "form_values": {},
            "reason": "",
            **status_ctx,
        },
    )


@admin_router.post("/config", response_class=HTMLResponse)
async def admin_config_update(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Admin panel update")

    try:
        async with async_session_factory() as session:
            values = {key: form[key] for key in public_config_keys() if key in form}
            await set_admin_config_values(
                session,
                values,
                changed_by=identity,
                reason=reason,
            )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            config_sections = build_admin_config_sections(configs.values())
            status_ctx = await _status_strip_context(session, configs=configs)
        return templates.TemplateResponse(
            request,
            "admin/config.html",
            {
                "identity": identity,
                "active": "config",
                "configs": configs,
                "config_sections": config_sections,
                "error": str(exc),
                "message": None,
                "form_values": form,
                "reason": reason,
                **status_ctx,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await _notify_gateway_config_reload()
    return RedirectResponse("/admin/config", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.get("/emergency", response_class=HTMLResponse)
async def admin_emergency(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        status_ctx = await _status_strip_context(session, configs=configs)

    kill_switch = configs["killSwitchEnabled"].value == "true"
    current_mode = configs["systemMode"].value

    return templates.TemplateResponse(
        request,
        "admin/emergency.html",
        {
            "identity": identity,
            "active": "emergency",
            "configs": configs,
            "kill_switch": kill_switch,
            "current_mode": current_mode,
            "error": None,
            "message": None,
            "submitted_reason": "",
            **status_ctx,
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

    try:
        async with async_session_factory() as session:
            await _apply_emergency_action(
                session,
                action,
                changed_by=identity,
                reason=reason,
            )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            status_ctx = await _status_strip_context(session, configs=configs)
        kill_switch = configs["killSwitchEnabled"].value == "true"
        current_mode = configs["systemMode"].value
        return templates.TemplateResponse(
            request,
            "admin/emergency.html",
            {
                "identity": identity,
                "active": "emergency",
                "configs": configs,
                "kill_switch": kill_switch,
                "current_mode": current_mode,
                "error": str(exc),
                "message": None,
                "submitted_reason": reason,
                **status_ctx,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse("/admin/emergency", status_code=status.HTTP_303_SEE_OTHER)


async def _apply_emergency_action(
    session: Any,
    action: str,
    *,
    changed_by: str,
    reason: str,
) -> None:
    if action in ("force-observe", "force-paper"):
        # v2: "force-paper" → systemMode=OBSERVE_ONLY (analiz sürer, emir yok).
        await set_admin_config_value(
            session,
            "systemMode",
            "OBSERVE_ONLY",
            changed_by=changed_by,
            reason=reason,
        )
        return
    if action == "enable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            True,
            changed_by=changed_by,
            reason=reason,
        )
        return
    if action == "disable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            False,
            changed_by=changed_by,
            reason=reason,
        )
        return
    raise ValueError(f"Unsupported emergency action: {action}")


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
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    if key in RISKY_CONFIG_KEYS:
        await notification_service.send(
            "warning",
            "Yönetim yapılandırması değişti",
            {"Anahtar": key, "Değer": item.display_value, "Kullanıcı": identity},
            event_key=f"admin-config:{key}:{item.display_value}",
        )
    return _config_dict(item)


@admin_api_router.put("/config")
async def admin_api_update_config_batch(
    request: Request, body: AdminConfigBatchUpdate
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            items = await set_admin_config_values(
                session,
                body.values,
                changed_by=identity,
                reason=body.reason,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return {"status": "ok", "updated": [_config_dict(item) for item in items]}


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
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await notification_service.send(
        "warning",
        "Acil işlem uygulandı",
        {"İşlem": action, "Kullanıcı": identity},
        event_key=f"admin-emergency:{action}",
    )
    return {"status": "ok", "action": action}
