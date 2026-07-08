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
    "max_orders_per_day",
    "max_orders_per_symbol_per_day",
    "min_confidence_for_buy",
    "min_confidence_for_sell",
    "max_natr_for_buy",
    "max_depth_queue_drop_pct_for_buy",
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
    "max_order_value_tl": float,
    "max_qty_per_order": float,
    "max_position_value_per_symbol": float,
    "max_orders_per_day": int,
    "max_orders_per_symbol_per_day": int,
    "min_confidence_for_buy": float,
    "min_confidence_for_sell": float,
    "max_natr_for_buy": float,
    "max_depth_queue_drop_pct_for_buy": float,
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
        "max_order_value_tl": 500.0,
        "max_qty_per_order": 1.0,
        "max_position_value_per_symbol": 1000.0,
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
        "max_order_value_tl": 1000.0,
        "max_qty_per_order": 3.0,
        "max_position_value_per_symbol": 3000.0,
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
        "max_order_value_tl": 3000.0,
        "max_qty_per_order": 10.0,
        "max_position_value_per_symbol": 10000.0,
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
        "max_order_value_tl": 5000.0,
        "max_qty_per_order": 20.0,
        "max_position_value_per_symbol": 15000.0,
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


def _builtin_profile_instance(code: str) -> TradeProfile:
    """A transient (unpersisted) TradeProfile built from BUILTIN_PROFILES —
    last-resort fallback so get_active_profile() never returns None."""
    data = dict(BUILTIN_PROFILES[code])
    return TradeProfile(code=code, is_builtin=True, is_enabled=True, **data)


def get_static_default_profile() -> TradeProfile:
    """Transient NORMAL profile for contexts with no DB access at all
    (e.g. build_static_bot_runtime_config's DB-unreachable fallback)."""
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
        session.add(TradeProfile(code=code, is_builtin=True, is_enabled=True, **BUILTIN_PROFILES[code]))
    try:
        await session.commit()
    except IntegrityError:
        # Another concurrent caller already seeded — fine, back off.
        await session.rollback()


async def list_profiles(session: AsyncSession) -> list[TradeProfile]:
    await ensure_builtin_profiles_seeded(session)
    stmt = select(TradeProfile).order_by(TradeProfile.is_builtin.desc(), TradeProfile.code.asc())
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
    session.add(ConfigAuditLog(
        key=f"trade_profile:{code}",
        old_value=None,
        new_value="created",
        changed_by=changed_by,
        reason=f"Created trade profile {code}",
    ))
    await session.commit()
    await session.refresh(profile)
    return profile


def profile_requires_confirmation(old: TradeProfile, changes: dict[str, Any]) -> bool:
    """True if ``changes`` applied to ``old`` loosen a safety-relevant field."""

    def _increased(field: str) -> bool:
        return field in changes and float(changes[field]) > float(getattr(old, field))

    def _decreased(field: str) -> bool:
        return field in changes and float(changes[field]) < float(getattr(old, field))

    if any(_increased(f) for f in ("max_order_value_tl", "max_qty_per_order", "max_orders_per_day")):
        return True
    if any(_decreased(f) for f in ("min_confidence_for_buy", "min_confidence_for_sell")):
        return True
    if changes.get("allow_real_live") is True and not old.allow_real_live:
        return True
    if changes.get("require_alpha_trend_alignment") is False and old.require_alpha_trend_alignment:
        return True
    if changes.get("require_indicator_consensus_alignment") is False and old.require_indicator_consensus_alignment:
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

    if profile_requires_confirmation(profile, changes) and confirmation != RISKY_CONFIRMATION:
        raise ValueError(f"This change requires confirmation={RISKY_CONFIRMATION}")

    for field, value in changes.items():
        setattr(profile, field, value)

    session.add(ConfigAuditLog(
        key=f"trade_profile:{code}",
        old_value="updated",
        new_value=str(changes),
        changed_by=changed_by,
        reason=reason or "Trade profile update",
    ))
    await session.commit()
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
        **{field: getattr(source, field) for field in EDITABLE_FIELDS if field not in ("name", "description", "risk_level")},
    )
    session.add(clone)
    session.add(ConfigAuditLog(
        key=f"trade_profile:{new_code}",
        old_value=None,
        new_value=f"cloned from {source_code}",
        changed_by=changed_by,
        reason=f"Cloned trade profile {source_code} -> {new_code}",
    ))
    await session.commit()
    await session.refresh(clone)
    return clone


async def disable_profile(session: AsyncSession, code: str, *, changed_by: str) -> TradeProfile:
    profile = await get_profile(session, code)
    if profile is None:
        raise ValueError(f"Unknown trade profile: {code}")
    if profile.is_default:
        raise ValueError(f"Cannot disable the default fallback profile: {code}")
    active_code = await _read_active_profile_code(session)
    if active_code == code:
        raise ValueError(f"Cannot disable the currently active profile: {code}")

    profile.is_enabled = False
    session.add(ConfigAuditLog(
        key=f"trade_profile:{code}",
        old_value="enabled",
        new_value="disabled",
        changed_by=changed_by,
        reason="Disabled trade profile",
    ))
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

    session.add(ConfigAuditLog(
        key=f"trade_profile:{code}",
        old_value="exists",
        new_value="deleted",
        changed_by=changed_by,
        reason="Deleted trade profile",
    ))
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
        session.add(ConfigAuditLog(
            key=ACTIVE_PROFILE_CONFIG_KEY,
            old_value=old_code,
            new_value=code,
            changed_by=changed_by,
            reason=reason or f"Activated trade profile {code}",
        ))

    await session.commit()
    return profile
