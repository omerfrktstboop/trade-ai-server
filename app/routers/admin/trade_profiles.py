"""Admin trade-profile routes (HTML pages + JSON API)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.db.session import async_session_factory
from app.models.db import (
    TradeProfile,
)
from app.services.trade_profile import (
    EDITABLE_FIELDS,
    FIELD_TYPES,
    RISKY_CONFIRMATION as PROFILE_RISKY_CONFIRMATION,
    activate_profile,
    clone_profile,
    create_profile,
    delete_profile,
    disable_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    update_profile,
)

from app.routers.admin._shared import (
    admin_router,
    admin_api_router,
    templates,
    require_admin,
    _to_float,
    _status_strip_context,
    _notify_gateway_config_reload,
)


class TradeProfileFieldsBody(BaseModel):
    name: str | None = None
    description: str | None = None
    risk_level: str | None = Field(None, alias="riskLevel")
    allowed_modes: str | None = Field(None, alias="allowedModes")
    max_order_value_tl: Decimal | None = Field(None, alias="maxOrderValueTl")
    max_qty_per_order: int | None = Field(None, alias="maxQtyPerOrder")
    max_position_value_per_symbol: Decimal | None = Field(
        None, alias="maxPositionValuePerSymbol"
    )
    risk_per_trade_pct: Decimal | None = Field(None, alias="riskPerTradePct")
    max_cash_utilization_pct: Decimal | None = Field(
        None, alias="maxCashUtilizationPct"
    )
    max_account_exposure_pct: Decimal | None = Field(
        None, alias="maxAccountExposurePct"
    )
    min_order_value_tl: Decimal | None = Field(None, alias="minOrderValueTl")
    min_stop_distance_pct: Decimal | None = Field(None, alias="minStopDistancePct")
    max_stop_distance_pct: Decimal | None = Field(None, alias="maxStopDistancePct")
    minimum_stop_slippage_pct: Decimal | None = Field(
        None, alias="minimumStopSlippagePct"
    )
    maximum_stop_slippage_pct: Decimal | None = Field(
        None, alias="maximumStopSlippagePct"
    )
    profile_stop_slippage_pct: Decimal | None = Field(
        None, alias="profileStopSlippagePct"
    )
    max_account_data_age_seconds: Decimal | None = Field(
        None, alias="maxAccountDataAgeSeconds"
    )
    max_orders_per_day: int | None = Field(None, alias="maxOrdersPerDay")
    max_orders_per_symbol_per_day: int | None = Field(
        None, alias="maxOrdersPerSymbolPerDay"
    )
    min_confidence_for_buy: float | None = Field(None, alias="minConfidenceForBuy")
    min_confidence_for_sell: float | None = Field(None, alias="minConfidenceForSell")
    max_natr_for_buy: float | None = Field(None, alias="maxNatrForBuy")
    max_depth_queue_drop_pct_for_buy: float | None = Field(
        None, alias="maxDepthQueueDropPctForBuy"
    )
    max_spread_pct_for_buy: float | None = Field(None, alias="maxSpreadPctForBuy")
    min_depth_bid_ask_ratio_top10_for_buy: float | None = Field(
        None, alias="minDepthBidAskRatioTop10ForBuy"
    )
    max_depth_sell_pressure_score_for_buy: float | None = Field(
        None, alias="maxDepthSellPressureScoreForBuy"
    )
    block_buy_on_strong_sell_pressure: bool | None = Field(
        None, alias="blockBuyOnStrongSellPressure"
    )
    block_buy_on_near_ask_wall: bool | None = Field(None, alias="blockBuyOnNearAskWall")
    near_wall_distance_pct: float | None = Field(None, alias="nearWallDistancePct")
    require_alpha_trend_alignment: bool | None = Field(
        None, alias="requireAlphaTrendAlignment"
    )
    require_indicator_consensus_alignment: bool | None = Field(
        None, alias="requireIndicatorConsensusAlignment"
    )
    allow_sell_long_term: bool | None = Field(None, alias="allowSellLongTerm")
    allow_short_selling: bool | None = Field(None, alias="allowShortSelling")
    allow_real_live: bool | None = Field(None, alias="allowRealLive")
    allow_demo_live: bool | None = Field(None, alias="allowDemoLive")
    allow_margin_buying: bool | None = Field(None, alias="allowMarginBuying")
    scan_interval_minutes: int | None = Field(None, alias="scanIntervalMinutes")
    max_fetch_loop_per_session: int | None = Field(None, alias="maxFetchLoopPerSession")
    order_time_in_force: str | None = Field(None, alias="orderTimeInForce")
    indicator_period: str | None = Field(None, alias="indicatorPeriod")

    model_config = {"populate_by_name": True}


class TradeProfileCreateBody(TradeProfileFieldsBody):
    code: str


class TradeProfileUpdateBody(TradeProfileFieldsBody):
    reason: str | None = None
    confirmation: str | None = None


class TradeProfileActivateBody(BaseModel):
    reason: str | None = None
    confirmation: str | None = None


class TradeProfileCloneBody(BaseModel):
    new_code: str = Field(alias="newCode")
    new_name: str = Field(alias="newName")

    model_config = {"populate_by_name": True}


def _parse_profile_form_fields(form: Any) -> dict[str, Any]:
    """Extract EDITABLE_FIELDS present in an HTML form, cast per FIELD_TYPES.

    Bool fields are rendered as <select>true/false</select> in the template
    (not checkboxes) so they're always present and unambiguous for both
    create (full form) and update (may omit unchanged fields).
    """
    changes: dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        raw = form.get(field)
        if raw is None or raw == "":
            continue
        field_type = FIELD_TYPES[field]
        if field_type is bool:
            changes[field] = str(raw).strip().lower() in ("true", "1", "yes", "on")
        elif field_type is float:
            value = _to_float(raw)
            if value is not None:
                changes[field] = value
        elif field_type is Decimal:
            changes[field] = Decimal(str(raw))
        elif field_type is int:
            value = _to_float(raw)
            if value is not None:
                changes[field] = int(value)
        else:
            changes[field] = str(raw).strip()
    return changes


async def _trade_profiles_page(
    request: Request,
    identity: str,
    *,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    async with async_session_factory() as session:
        profiles = await list_profiles(session)
        active = await get_active_profile(session)
        status_ctx = await _status_strip_context(session, profile=active)

    return templates.TemplateResponse(
        request,
        "admin/trade_profiles.html",
        {
            "identity": identity,
            "active": "trade-profiles",
            "profiles": profiles,
            "active_code": active.code,
            "active_profile": active,
            "confirmation": PROFILE_RISKY_CONFIRMATION,
            "error": error,
            "message": message,
            **status_ctx,
        },
    )


@admin_router.get("/trade-profiles", response_class=HTMLResponse)
async def admin_trade_profiles(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _trade_profiles_page(request, identity)


@admin_router.post("/trade-profiles/create")
async def admin_trade_profiles_create(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    code = str(form.get("code") or "").strip().upper()
    name = str(form.get("name") or "").strip()
    description = str(form.get("description") or "")
    risk_level = str(form.get("risk_level") or "MEDIUM").strip().upper()
    changes = _parse_profile_form_fields(form)
    for field in ("name", "description", "risk_level"):
        changes.pop(field, None)

    try:
        async with async_session_factory() as session:
            await create_profile(
                session,
                code=code,
                name=name,
                description=description,
                risk_level=risk_level,
                changed_by=identity,
                **changes,
            )
    except (ValueError, TypeError) as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/update")
async def admin_trade_profiles_update(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Trade profile update")
    confirmation = str(form.get("confirmation") or "")
    changes = _parse_profile_form_fields(form)

    try:
        async with async_session_factory() as session:
            await update_profile(
                session,
                code,
                changes,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/activate")
async def admin_trade_profiles_activate(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or f"Activated {code}")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            await activate_profile(
                session,
                code,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/clone")
async def admin_trade_profiles_clone(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    new_code = str(form.get("new_code") or "").strip().upper()
    new_name = str(form.get("new_name") or "").strip()

    try:
        async with async_session_factory() as session:
            await clone_profile(
                session, code, new_code=new_code, new_name=new_name, changed_by=identity
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/disable")
async def admin_trade_profiles_disable(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await disable_profile(session, code, changed_by=identity)
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/delete")
async def admin_trade_profiles_delete(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await delete_profile(session, code, changed_by=identity)
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


def _trade_profile_dict(profile: TradeProfile, active_code: str) -> dict[str, Any]:
    return {
        "code": profile.code,
        "name": profile.name,
        "description": profile.description,
        "riskLevel": profile.risk_level,
        "isEnabled": profile.is_enabled,
        "isDefault": profile.is_default,
        "isBuiltin": profile.is_builtin,
        "isActive": profile.code == active_code,
        "allowedModes": profile.allowed_modes,
        "maxOrderValueTl": profile.max_order_value_tl,
        "maxQtyPerOrder": profile.max_qty_per_order,
        "maxPositionValuePerSymbol": profile.max_position_value_per_symbol,
        "riskPerTradePct": profile.risk_per_trade_pct,
        "maxCashUtilizationPct": profile.max_cash_utilization_pct,
        "maxAccountExposurePct": profile.max_account_exposure_pct,
        "minOrderValueTl": profile.min_order_value_tl,
        "minStopDistancePct": profile.min_stop_distance_pct,
        "maxStopDistancePct": profile.max_stop_distance_pct,
        "minimumStopSlippagePct": profile.minimum_stop_slippage_pct,
        "maximumStopSlippagePct": profile.maximum_stop_slippage_pct,
        "profileStopSlippagePct": profile.profile_stop_slippage_pct,
        "maxAccountDataAgeSeconds": profile.max_account_data_age_seconds,
        "maxOrdersPerDay": profile.max_orders_per_day,
        "maxOrdersPerSymbolPerDay": profile.max_orders_per_symbol_per_day,
        "minConfidenceForBuy": profile.min_confidence_for_buy,
        "minConfidenceForSell": profile.min_confidence_for_sell,
        "maxNatrForBuy": profile.max_natr_for_buy,
        "maxDepthQueueDropPctForBuy": profile.max_depth_queue_drop_pct_for_buy,
        "maxSpreadPctForBuy": profile.max_spread_pct_for_buy,
        "minDepthBidAskRatioTop10ForBuy": profile.min_depth_bid_ask_ratio_top10_for_buy,
        "maxDepthSellPressureScoreForBuy": profile.max_depth_sell_pressure_score_for_buy,
        "blockBuyOnStrongSellPressure": profile.block_buy_on_strong_sell_pressure,
        "blockBuyOnNearAskWall": profile.block_buy_on_near_ask_wall,
        "nearWallDistancePct": profile.near_wall_distance_pct,
        "requireAlphaTrendAlignment": profile.require_alpha_trend_alignment,
        "requireIndicatorConsensusAlignment": profile.require_indicator_consensus_alignment,
        "allowSellLongTerm": profile.allow_sell_long_term,
        "allowShortSelling": profile.allow_short_selling,
        "allowRealLive": profile.allow_real_live,
        "allowDemoLive": profile.allow_demo_live,
        "allowMarginBuying": profile.allow_margin_buying,
        "scanIntervalMinutes": profile.scan_interval_minutes,
        "maxFetchLoopPerSession": profile.max_fetch_loop_per_session,
        "orderTimeInForce": profile.order_time_in_force,
        "indicatorPeriod": profile.indicator_period,
    }


@admin_api_router.get("/trade-profiles")
async def admin_api_list_trade_profiles(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        profiles = await list_profiles(session)
        active = await get_active_profile(session)
    return [_trade_profile_dict(p, active.code) for p in profiles]


@admin_api_router.post("/trade-profiles")
async def admin_api_create_trade_profile(
    request: Request, body: TradeProfileCreateBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    fields = body.model_dump(
        exclude={"code", "name", "description", "risk_level"}, exclude_none=True
    )
    try:
        async with async_session_factory() as session:
            profile = await create_profile(
                session,
                code=body.code,
                name=body.name or body.code,
                description=body.description or "",
                risk_level=body.risk_level or "MEDIUM",
                changed_by=identity,
                **fields,
            )
            active = await get_active_profile(session)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.get("/trade-profiles/{code}")
async def admin_api_get_trade_profile(request: Request, code: str) -> dict[str, Any]:
    await require_admin(request)
    async with async_session_factory() as session:
        profile = await get_profile(session, code)
        if profile is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown trade profile: {code}"
            )
        active = await get_active_profile(session)
    return _trade_profile_dict(profile, active.code)


@admin_api_router.put("/trade-profiles/{code}")
async def admin_api_update_trade_profile(
    request: Request, code: str, body: TradeProfileUpdateBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    changes = body.model_dump(exclude={"reason", "confirmation"}, exclude_none=True)
    try:
        async with async_session_factory() as session:
            profile = await update_profile(
                session,
                code,
                changes,
                changed_by=identity,
                reason=body.reason,
                confirmation=body.confirmation,
            )
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.post("/trade-profiles/{code}/activate")
async def admin_api_activate_trade_profile(
    request: Request, code: str, body: TradeProfileActivateBody | None = None
) -> dict[str, Any]:
    identity = await require_admin(request)
    payload = body or TradeProfileActivateBody()
    try:
        async with async_session_factory() as session:
            profile = await activate_profile(
                session,
                code,
                changed_by=identity,
                reason=payload.reason,
                confirmation=payload.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, code)


@admin_api_router.post("/trade-profiles/{code}/clone")
async def admin_api_clone_trade_profile(
    request: Request, code: str, body: TradeProfileCloneBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            clone = await clone_profile(
                session,
                code,
                new_code=body.new_code,
                new_name=body.new_name,
                changed_by=identity,
            )
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(clone, active.code)


@admin_api_router.post("/trade-profiles/{code}/disable")
async def admin_api_disable_trade_profile(
    request: Request, code: str
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            profile = await disable_profile(session, code, changed_by=identity)
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.delete("/trade-profiles/{code}")
async def admin_api_delete_trade_profile(request: Request, code: str) -> dict[str, str]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await delete_profile(session, code, changed_by=identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return {"status": "ok", "code": code}
