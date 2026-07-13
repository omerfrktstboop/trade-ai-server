"""Trade profiles — named risk/behavior presets driving both RiskEngine and
the Matriks bot's runtime config.

Deliberately does NOT import app.services.admin_config — admin_config will
import this module (to resolve the active profile when building RiskConfig /
bot config), so importing it back here would create a cycle. The
``activeTradeProfileCode`` pointer is read/written directly against
``SystemConfig`` instead of going through admin_config's CONFIG_DEFINITIONS
machinery (validating a profile code / risk level needs DB access that
admin_config's synchronous ``_serialize_value`` can't do).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ConfigAuditLog, SystemConfig, TradeProfile

RISKY_CONFIRMATION = "CONFIRM"
ACTIVE_PROFILE_CONFIG_KEY = "activeTradeProfileCode"

# Fields editable via update_profile / clone_profile — everything except
# identity (code/is_builtin) and bookkeeping (id/created_at/updated_at).
EDITABLE_FIELDS = (
    "name",
    "description",
    "risk_level",
    "allowed_modes",
    "max_order_value_tl",
    "max_qty_per_order",
    "max_position_value_per_symbol",
    "risk_per_trade_pct",
    "max_cash_utilization_pct",
    "max_account_exposure_pct",
    "min_order_value_tl",
    "min_stop_distance_pct",
    "max_stop_distance_pct",
    "minimum_stop_slippage_pct",
    "maximum_stop_slippage_pct",
    "profile_stop_slippage_pct",
    "max_account_data_age_seconds",
    "max_orders_per_day",
    "max_orders_per_symbol_per_day",
    "min_confidence_for_buy",
    "min_confidence_for_sell",
    "max_natr_for_buy",
    "max_depth_queue_drop_pct_for_buy",
    "max_spread_pct_for_buy",
    "min_depth_bid_ask_ratio_top10_for_buy",
    "max_depth_sell_pressure_score_for_buy",
    "block_buy_on_strong_sell_pressure",
    "block_buy_on_near_ask_wall",
    "near_wall_distance_pct",
    "require_alpha_trend_alignment",
    "require_indicator_consensus_alignment",
    "allow_sell_long_term",
    "allow_short_selling",
    "allow_real_live",
    "allow_demo_live",
    "scan_interval_minutes",
    "max_fetch_loop_per_session",
    "order_time_in_force",
    "indicator_period",
)

# Python type for each editable field — reused by admin.py's HTML form
# parsing (JSON API requests are typed directly via Pydantic).
FIELD_TYPES: dict[str, type] = {
    "name": str,
    "description": str,
    "risk_level": str,
    "allowed_modes": str,
    "max_order_value_tl": Decimal,
    "max_qty_per_order": int,
    "max_position_value_per_symbol": Decimal,
    "risk_per_trade_pct": Decimal,
    "max_cash_utilization_pct": Decimal,
    "max_account_exposure_pct": Decimal,
    "min_order_value_tl": Decimal,
    "min_stop_distance_pct": Decimal,
    "max_stop_distance_pct": Decimal,
    "minimum_stop_slippage_pct": Decimal,
    "maximum_stop_slippage_pct": Decimal,
    "profile_stop_slippage_pct": Decimal,
    "max_account_data_age_seconds": Decimal,
    "max_orders_per_day": int,
    "max_orders_per_symbol_per_day": int,
    "min_confidence_for_buy": float,
    "min_confidence_for_sell": float,
    "max_natr_for_buy": float,
    "max_depth_queue_drop_pct_for_buy": float,
    "max_spread_pct_for_buy": float,
    "min_depth_bid_ask_ratio_top10_for_buy": float,
    "max_depth_sell_pressure_score_for_buy": float,
    "block_buy_on_strong_sell_pressure": bool,
    "block_buy_on_near_ask_wall": bool,
    "near_wall_distance_pct": float,
    "require_alpha_trend_alignment": bool,
    "require_indicator_consensus_alignment": bool,
    "allow_sell_long_term": bool,
    "allow_short_selling": bool,
    "allow_real_live": bool,
    "allow_demo_live": bool,
    "scan_interval_minutes": int,
    "max_fetch_loop_per_session": int,
    "order_time_in_force": str,
    "indicator_period": str,
}

# ── Built-in seed profiles ───────────────────────────────────────────────────

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "CONSERVATIVE": {
        "name": "Conservative",
        "description": "Sıkı limitler, yüksek güven eşiği — küçük ve seyrek işlemler.",
        "risk_level": "LOW",
        "is_default": False,
        "max_order_value_tl": Decimal("500"),
        "max_qty_per_order": 1,
        "max_position_value_per_symbol": Decimal("1000"),
        "max_orders_per_day": 1,
        "max_orders_per_symbol_per_day": 1,
        "min_confidence_for_buy": 85.0,
        "min_confidence_for_sell": 80.0,
        "max_natr_for_buy": 4.0,
        "max_depth_queue_drop_pct_for_buy": 20.0,
        "require_alpha_trend_alignment": True,
        "require_indicator_consensus_alignment": True,
        "allow_sell_long_term": False,
        "allow_short_selling": False,
        "allow_real_live": True,
        "allow_demo_live": True,
        "scan_interval_minutes": 60,
        "max_fetch_loop_per_session": 3,
        "order_time_in_force": "Day",
        "indicator_period": "Min5",
    },
    "NORMAL": {
        "name": "Normal",
        "description": "Varsayılan dengeli profil.",
        "risk_level": "MEDIUM",
        "is_default": True,
        "max_order_value_tl": Decimal("1000"),
        "max_qty_per_order": 3,
        "max_position_value_per_symbol": Decimal("3000"),
        "max_orders_per_day": 3,
        "max_orders_per_symbol_per_day": 1,
        "min_confidence_for_buy": 75.0,
        "min_confidence_for_sell": 70.0,
        "max_natr_for_buy": 8.0,
        "max_depth_queue_drop_pct_for_buy": 35.0,
        "require_alpha_trend_alignment": True,
        "require_indicator_consensus_alignment": True,
        "allow_sell_long_term": False,
        "allow_short_selling": False,
        "allow_real_live": True,
        "allow_demo_live": True,
        "scan_interval_minutes": 30,
        "max_fetch_loop_per_session": 3,
        "order_time_in_force": "Day",
        "indicator_period": "Min5",
    },
    "AGGRESSIVE": {
        "name": "Aggressive",
        "description": "Gevşek limitler, düşük güven eşiği — sık ve büyük işlemler.",
        "risk_level": "HIGH",
        "is_default": False,
        "max_order_value_tl": Decimal("3000"),
        "max_qty_per_order": 10,
        "max_position_value_per_symbol": Decimal("10000"),
        "max_orders_per_day": 8,
        "max_orders_per_symbol_per_day": 3,
        "min_confidence_for_buy": 65.0,
        "min_confidence_for_sell": 60.0,
        "max_natr_for_buy": 12.0,
        "max_depth_queue_drop_pct_for_buy": 50.0,
        "require_alpha_trend_alignment": False,
        "require_indicator_consensus_alignment": True,
        "allow_sell_long_term": False,
        "allow_short_selling": False,
        "allow_real_live": False,
        "allow_demo_live": True,
        "scan_interval_minutes": 15,
        "max_fetch_loop_per_session": 3,
        "order_time_in_force": "Day",
        "indicator_period": "Min5",
    },
    "HIGH_RISK": {
        "name": "High Risk / Experimental",
        "description": "En gevşek limitler — sadece deney/test amaçlı, REAL_LIVE varsayılan kapalı.",
        "risk_level": "EXTREME",
        "is_default": False,
        "max_order_value_tl": Decimal("5000"),
        "max_qty_per_order": 20,
        "max_position_value_per_symbol": Decimal("15000"),
        "max_orders_per_day": 15,
        "max_orders_per_symbol_per_day": 5,
        "min_confidence_for_buy": 55.0,
        "min_confidence_for_sell": 55.0,
        "max_natr_for_buy": 20.0,
        "max_depth_queue_drop_pct_for_buy": 70.0,
        "require_alpha_trend_alignment": False,
        "require_indicator_consensus_alignment": False,
        "allow_sell_long_term": False,
        "allow_short_selling": False,
        "allow_real_live": False,
        "allow_demo_live": True,
        "scan_interval_minutes": 5,
        "max_fetch_loop_per_session": 3,
        "order_time_in_force": "Day",
        "indicator_period": "Min5",
    },
}

_SIZING_DEFAULTS: dict[str, dict[str, Any]] = {
    "CONSERVATIVE": {
        "risk_per_trade_pct": Decimal("0.25"),
        "max_cash_utilization_pct": Decimal("15"),
        "max_account_exposure_pct": Decimal("30"),
    },
    "NORMAL": {
        "risk_per_trade_pct": Decimal("0.50"),
        "max_cash_utilization_pct": Decimal("25"),
        "max_account_exposure_pct": Decimal("50"),
    },
    "AGGRESSIVE": {
        "risk_per_trade_pct": Decimal("0.75"),
        "max_cash_utilization_pct": Decimal("35"),
        "max_account_exposure_pct": Decimal("65"),
    },
    "HIGH_RISK": {
        "risk_per_trade_pct": Decimal("1"),
        "max_cash_utilization_pct": Decimal("50"),
        "max_account_exposure_pct": Decimal("75"),
    },
}
for _profile_code, _profile_defaults in _SIZING_DEFAULTS.items():
    BUILTIN_PROFILES[_profile_code].update(
        **_profile_defaults,
        min_order_value_tl=Decimal("1"),
        min_stop_distance_pct=Decimal("0.10"),
        max_stop_distance_pct=Decimal("10"),
        minimum_stop_slippage_pct=Decimal("0.05"),
        maximum_stop_slippage_pct=Decimal("1"),
        profile_stop_slippage_pct=Decimal("0.20"),
        max_account_data_age_seconds=Decimal("60"),
    )


def _builtin_profile_instance(code: str) -> TradeProfile:
    """A transient (unpersisted) TradeProfile built from BUILTIN_PROFILES —
    last-resort fallback so get_active_profile() never returns None."""
    data = dict(BUILTIN_PROFILES[code])
    return TradeProfile(code=code, is_builtin=True, is_enabled=True, **data)


def get_static_default_profile() -> TradeProfile:
    """Transient NORMAL profile for contexts with no DB access at all."""
    return _builtin_profile_instance("NORMAL")


async def ensure_builtin_profiles_seeded(session: AsyncSession) -> None:
    """Idempotently insert the 4 built-in profiles if the table is empty.

    Called from app/db/init_db.py (dev) AND defensively from list_profiles/
    get_active_profile (the only seed path in prod — this repo has no
    Alembic migrations, so init_db() never runs outside APP_ENV=development).
    """
    existing_codes = set(
        (await session.execute(select(TradeProfile.code))).scalars().all()
    )
    missing = [code for code in BUILTIN_PROFILES if code not in existing_codes]
    if not missing:
        return
    for code in missing:
        session.add(
            TradeProfile(
                code=code, is_builtin=True, is_enabled=True, **BUILTIN_PROFILES[code]
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        # Another concurrent caller already seeded — fine, back off.
        await session.rollback()


async def list_profiles(session: AsyncSession) -> list[TradeProfile]:
    await ensure_builtin_profiles_seeded(session)
    stmt = select(TradeProfile).order_by(
        TradeProfile.is_builtin.desc(), TradeProfile.code.asc()
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_profile(session: AsyncSession, code: str) -> TradeProfile | None:
    code = code.strip().upper()
    stmt = select(TradeProfile).where(TradeProfile.code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _read_active_profile_code(session: AsyncSession) -> str | None:
    stmt = select(SystemConfig).where(SystemConfig.key == ACTIVE_PROFILE_CONFIG_KEY)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row.value if row else None


async def get_active_profile(session: AsyncSession) -> TradeProfile:
    """Resolve the system-wide active profile. Never returns None."""
    await ensure_builtin_profiles_seeded(session)

    code = await _read_active_profile_code(session)
    if code:
        profile = await get_profile(session, code)
        if profile is not None and profile.is_enabled:
            return profile

    default_stmt = select(TradeProfile).where(
        TradeProfile.is_default.is_(True), TradeProfile.is_enabled.is_(True)
    )
    default_profile = (await session.execute(default_stmt)).scalar_one_or_none()
    if default_profile is not None:
        return default_profile

    return _builtin_profile_instance("NORMAL")


async def create_profile(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    changed_by: str,
    description: str = "",
    risk_level: str = "MEDIUM",
    **fields: Any,
) -> TradeProfile:
    code = code.strip().upper()
    if not code:
        raise ValueError("Trade profile code cannot be empty")
    if await get_profile(session, code) is not None:
        raise ValueError(f"Trade profile code already exists: {code}")

    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown trade profile fields: {sorted(unknown)}")

    # Formdan gelmeyen zorunlu limit alanlarını NORMAL profilinin dengeli
    # değerleriyle doldur — kısmi bir admin formu NOT NULL ihlaliyle
    # patlamak yerine güvenli varsayılanlarla oluşturulur. Meta alanlar
    # (name/description/risk_level/is_default) ayrı keyword'lerle geldiği
    # için buradan dışlanır.
    _meta_fields = {"name", "description", "risk_level", "is_default"}
    normal_defaults = {
        key: value
        for key, value in BUILTIN_PROFILES["NORMAL"].items()
        if key in EDITABLE_FIELDS and key not in _meta_fields
    }
    fields = {**normal_defaults, **fields}

    profile = TradeProfile(
        code=code,
        name=name,
        description=description,
        risk_level=risk_level,
        is_builtin=False,
        is_enabled=True,
        is_default=False,
        **fields,
    )
    session.add(profile)
    session.add(
        ConfigAuditLog(
            key=f"trade_profile:{code}",
            old_value=None,
            new_value="created",
            changed_by=changed_by,
            reason=f"Created trade profile {code}",
        )
    )
    await session.commit()
    from app.services.decision_gate import decision_cache

    decision_cache.clear()
    await session.refresh(profile)
    return profile


def profile_requires_confirmation(old: TradeProfile, changes: dict[str, Any]) -> bool:
    """True if ``changes`` applied to ``old`` loosen a safety-relevant field."""

    def _decimal(value: Any) -> Decimal:
        return value if isinstance(value, Decimal) else Decimal(str(value))

    def _increased(field: str) -> bool:
        return field in changes and _decimal(changes[field]) > _decimal(
            getattr(old, field)
        )

    def _decreased(field: str) -> bool:
        return field in changes and _decimal(changes[field]) < _decimal(
            getattr(old, field)
        )

    if any(
        _increased(f)
        for f in (
            "max_order_value_tl",
            "max_qty_per_order",
            "max_position_value_per_symbol",
            "max_orders_per_day",
            "risk_per_trade_pct",
            "max_cash_utilization_pct",
            "max_account_exposure_pct",
            "max_stop_distance_pct",
            "max_account_data_age_seconds",
        )
    ):
        return True
    if any(
        _decreased(f)
        for f in (
            "min_confidence_for_buy",
            "min_confidence_for_sell",
            "min_stop_distance_pct",
            "minimum_stop_slippage_pct",
            "profile_stop_slippage_pct",
            "maximum_stop_slippage_pct",
        )
    ):
        return True
    if changes.get("allow_real_live") is True and not old.allow_real_live:
        return True
    if (
        changes.get("require_alpha_trend_alignment") is False
        and old.require_alpha_trend_alignment
    ):
        return True
    if (
        changes.get("require_indicator_consensus_alignment") is False
        and old.require_indicator_consensus_alignment
    ):
        return True
    return False


async def update_profile(
    session: AsyncSession,
    code: str,
    changes: dict[str, Any],
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
) -> TradeProfile:
    profile = await get_profile(session, code)
    if profile is None:
        raise ValueError(f"Unknown trade profile: {code}")

    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown trade profile fields: {sorted(unknown)}")

    if (
        profile_requires_confirmation(profile, changes)
        and confirmation != RISKY_CONFIRMATION
    ):
        raise ValueError(f"This change requires confirmation={RISKY_CONFIRMATION}")

    for field, value in changes.items():
        setattr(profile, field, value)
    profile.version = int(profile.version or 1) + 1

    session.add(
        ConfigAuditLog(
            key=f"trade_profile:{code}",
            old_value="updated",
            new_value=str(changes),
            changed_by=changed_by,
            reason=reason or "Trade profile update",
        )
    )
    await session.commit()
    from app.services.decision_gate import decision_cache

    decision_cache.clear()
    await session.refresh(profile)
    return profile


async def clone_profile(
    session: AsyncSession,
    source_code: str,
    *,
    new_code: str,
    new_name: str,
    changed_by: str,
) -> TradeProfile:
    source = await get_profile(session, source_code)
    if source is None:
        raise ValueError(f"Unknown trade profile: {source_code}")

    new_code = new_code.strip().upper()
    if not new_code:
        raise ValueError("Trade profile code cannot be empty")
    if await get_profile(session, new_code) is not None:
        raise ValueError(f"Trade profile code already exists: {new_code}")

    clone = TradeProfile(
        code=new_code,
        name=new_name,
        description=source.description,
        risk_level=source.risk_level,
        is_builtin=False,
        is_enabled=True,
        is_default=False,
        **{
            field: getattr(source, field)
            for field in EDITABLE_FIELDS
            if field not in ("name", "description", "risk_level")
        },
    )
    session.add(clone)
    session.add(
        ConfigAuditLog(
            key=f"trade_profile:{new_code}",
            old_value=None,
            new_value=f"cloned from {source_code}",
            changed_by=changed_by,
            reason=f"Cloned trade profile {source_code} -> {new_code}",
        )
    )
    await session.commit()
    await session.refresh(clone)
    return clone


async def disable_profile(
    session: AsyncSession, code: str, *, changed_by: str
) -> TradeProfile:
    profile = await get_profile(session, code)
    if profile is None:
        raise ValueError(f"Unknown trade profile: {code}")
    if profile.is_default:
        raise ValueError(f"Cannot disable the default fallback profile: {code}")
    active_code = await _read_active_profile_code(session)
    if active_code == code:
        raise ValueError(f"Cannot disable the currently active profile: {code}")

    profile.is_enabled = False
    session.add(
        ConfigAuditLog(
            key=f"trade_profile:{code}",
            old_value="enabled",
            new_value="disabled",
            changed_by=changed_by,
            reason="Disabled trade profile",
        )
    )
    await session.commit()
    await session.refresh(profile)
    return profile


async def delete_profile(session: AsyncSession, code: str, *, changed_by: str) -> None:
    profile = await get_profile(session, code)
    if profile is None:
        raise ValueError(f"Unknown trade profile: {code}")
    if profile.is_builtin:
        raise ValueError(f"Built-in trade profiles cannot be deleted: {code}")
    active_code = await _read_active_profile_code(session)
    if active_code == code:
        raise ValueError(f"Cannot delete the currently active profile: {code}")

    session.add(
        ConfigAuditLog(
            key=f"trade_profile:{code}",
            old_value="exists",
            new_value="deleted",
            changed_by=changed_by,
            reason="Deleted trade profile",
        )
    )
    await session.delete(profile)
    await session.commit()


async def activate_profile(
    session: AsyncSession,
    code: str,
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
) -> TradeProfile:
    profile = await get_profile(session, code)
    if profile is None or not profile.is_enabled:
        raise ValueError(f"Unknown or disabled trade profile: {code}")

    if profile.risk_level in {"HIGH", "EXTREME"} and confirmation != RISKY_CONFIRMATION:
        raise ValueError(
            f"Activating {code} ({profile.risk_level} risk) requires confirmation={RISKY_CONFIRMATION}"
        )

    stmt = select(SystemConfig).where(SystemConfig.key == ACTIVE_PROFILE_CONFIG_KEY)
    row = (await session.execute(stmt)).scalar_one_or_none()
    old_code = row.value if row else None
    if row is None:
        row = SystemConfig(
            key=ACTIVE_PROFILE_CONFIG_KEY,
            value=code,
            value_type="string",
            description="Currently active trade profile code",
        )
        session.add(row)
    else:
        row.value = code

    if old_code != code:
        session.add(
            ConfigAuditLog(
                key=ACTIVE_PROFILE_CONFIG_KEY,
                old_value=old_code,
                new_value=code,
                changed_by=changed_by,
                reason=reason or f"Activated trade profile {code}",
            )
        )

    await session.commit()
    if old_code != code:
        from app.services.decision_gate import decision_cache

        decision_cache.clear()
    return profile
