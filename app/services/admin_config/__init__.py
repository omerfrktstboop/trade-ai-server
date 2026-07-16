"""Runtime admin configuration backed by ``system_configs``.

Split into definitions.py (the ConfigDefinition table + admin-panel
section grouping), validation.py (value serialization + CONFIRM-required
rules), and store.py (DB reads/writes + composite resolvers). This
package re-exports everything under the original ``app.services.admin_config``
import path so existing callers don't need to change.
"""

from __future__ import annotations

from app.services.admin_config.definitions import (
    CONFIG_DEFINITIONS,
    CONFIG_LABELS,
    CONFIG_SECTION_DEFINITIONS,
    EMPTY_ALLOWED_CONFIG_KEYS,
    READ_ONLY_CONFIG_KEYS,
    RISKY_CONFIG_KEYS,
    RISKY_CONFIRMATION,
    SECRET_CONFIG_KEYS,
    AdminConfigItem,
    AdminConfigSection,
    ConfigDefinition,
    ConfigSectionDefinition,
    build_admin_config_sections,
    public_config_keys,
)
from app.services.admin_config.validation import (
    _ensure_allowed_key,
    _parse_bool,
    _requires_confirmation,
    _serialize_value,
)
from app.services.admin_config.store import (
    FeeConfig,
    build_runtime_risk_config,
    disarm_real_account,
    get_admin_config_value,
    get_ai_tool_calling_enabled,
    get_fee_config,
    get_manual_approval_allow_orders,
    get_market_session_close_time,
    get_outcome_maximum_observation_delay_seconds,
    get_portfolio_scan_interval_minutes,
    get_scanner_allow_orders,
    get_stop_guard_maximum_quote_age_seconds,
    get_system_mode,
    get_trading_mode_override,
    has_admin_config_row,
    is_auto_trade,
    is_kill_switch_enabled,
    is_scanner_runtime_enabled,
    list_admin_configs,
    set_admin_config_value,
    set_admin_config_values,
)

__all__ = [
    "CONFIG_DEFINITIONS",
    "CONFIG_LABELS",
    "CONFIG_SECTION_DEFINITIONS",
    "EMPTY_ALLOWED_CONFIG_KEYS",
    "READ_ONLY_CONFIG_KEYS",
    "RISKY_CONFIG_KEYS",
    "RISKY_CONFIRMATION",
    "SECRET_CONFIG_KEYS",
    "AdminConfigItem",
    "AdminConfigSection",
    "ConfigDefinition",
    "ConfigSectionDefinition",
    "build_admin_config_sections",
    "public_config_keys",
    "_ensure_allowed_key",
    "_parse_bool",
    "_requires_confirmation",
    "_serialize_value",
    "FeeConfig",
    "build_runtime_risk_config",
    "disarm_real_account",
    "get_system_mode",
    "is_auto_trade",
    "get_admin_config_value",
    "get_ai_tool_calling_enabled",
    "get_fee_config",
    "get_manual_approval_allow_orders",
    "get_market_session_close_time",
    "get_outcome_maximum_observation_delay_seconds",
    "get_portfolio_scan_interval_minutes",
    "get_scanner_allow_orders",
    "get_stop_guard_maximum_quote_age_seconds",
    "get_trading_mode_override",
    "has_admin_config_row",
    "is_kill_switch_enabled",
    "is_scanner_runtime_enabled",
    "list_admin_configs",
    "set_admin_config_value",
    "set_admin_config_values",
]
