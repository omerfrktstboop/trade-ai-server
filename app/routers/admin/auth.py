"""Admin login/logout routes."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings

from app.routers.admin._shared import (
    admin_router,
    templates,
    _make_admin_cookie,
    ADMIN_COOKIE_NAME,
    ADMIN_COOKIE_TTL_SECONDS,
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
