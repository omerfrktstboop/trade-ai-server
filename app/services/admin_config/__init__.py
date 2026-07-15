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
    build_runtime_risk_config,
    get_admin_config_value,
    get_trading_mode_override,
    has_admin_config_row,
    is_kill_switch_enabled,
    list_admin_configs,
    set_admin_config_value,
    set_admin_config_values,
)

__all__ = [
    "CONFIG_DEFINITIONS",
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
    "build_runtime_risk_config",
    "get_admin_config_value",
    "get_trading_mode_override",
    "has_admin_config_row",
    "is_kill_switch_enabled",
    "list_admin_configs",
    "set_admin_config_value",
    "set_admin_config_values",
]
