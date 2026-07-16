"""Admin panel and admin API routes.

Split by domain for maintainability: _shared.py holds the router objects,
templates, and cross-cutting helpers (auth, config lookup, status strip,
generic DB helpers); every other module imports from it and registers
routes onto the same admin_router/admin_api_router instances. Importing
each submodule here (for its side-effecting @admin_router.get(...)
decorators) and re-exporting the two router objects keeps
``from app.routers.admin import admin_api_router, admin_router`` working
unchanged for main.py.

Also re-exports internal names that tests import/call directly
(``from app.routers.admin import _local_time`` etc.) for backward
compatibility. Note this does NOT make ``monkeypatch.setattr(admin,
"gateway_client", ...)`` affect a submodule's own ``gateway_client``
binding - each submodule imports it independently, so patches must target
the submodule (e.g. ``app.routers.admin.dashboard``) directly.
"""

from __future__ import annotations

from app.services.notifications import notification_service  # noqa: F401

from app.routers.admin._shared import (  # noqa: F401
    admin_api_router,
    admin_router,
    templates,
    _local_time,
)

from app.routers.admin import (  # noqa: F401
    arming,
    auth,
    config_routes,
    dashboard,
    orders,
    positions,
    research,
    trade_profiles,
)
from app.routers.admin.research import (  # noqa: F401
    _research_rr_ratio,
    _research_rank_rows,
)

__all__ = ["admin_router", "admin_api_router"]
