"""FastMCP sargısı — registry'deki read-only araçları MCP protokolüyle dışa aç.

Aynı araç tanımları DeepSeek function-calling tarafından in-process
kullanılır; buradaki sunucu yalnızca dış istemciler (ör. Claude Desktop,
MCP inspector) içindir. ``/mcp`` mount'u admin API token'ı ile korunur.

Her MCP çağrısı da ``call_tool(caller="mcp")`` üzerinden geçer — böylece
timeout, sonuç kırpma ve ``tool_call_audits`` kaydı iki tüketici için tek
noktada uygulanır.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import logging
import typing
from typing import Any, Awaitable, Callable

from app.config import settings
from app.tools.registry import ToolSpec, call_tool, specs_for_audience

logger = logging.getLogger(__name__)


def _mcp_handler(spec: ToolSpec) -> Callable[..., Awaitable[Any]]:
    """Orijinal handler imzasını koruyan (şema üretimi için) audit'li sargı."""

    @functools.wraps(spec.handler)
    async def wrapper(**kwargs: Any) -> Any:
        result = await call_tool(spec.name, kwargs, caller="mcp")
        if "error" in result:
            # MCP istemcisine hata metni içerik olarak döner; exception
            # fırlatmak stream'i düşürür.
            return {"error": result["error"]}
        return result.get("result")

    # Katalog modülleri `from __future__ import annotations` kullandığı için
    # tip ipuçları string'dir; FastMCP'nin şema üretimi gerçek tipleri ister.
    hints = typing.get_type_hints(spec.handler)
    resolved_params = [
        param.replace(annotation=hints.get(param.name, param.annotation))
        for param in inspect.signature(spec.handler).parameters.values()
    ]
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        resolved_params, return_annotation=hints.get("return", Any)
    )
    wrapper.__annotations__ = {**hints}
    return wrapper


def build_mcp_server() -> Any:
    """Registry'den beslenen FastMCP örneği (HTTP'siz, test edilebilir)."""
    from mcp.server.fastmcp import FastMCP

    # streamable_http_path="/" — dış mount noktası app/main.py'de /mcp;
    # default "/mcp" bırakılırsa yol /mcp/mcp olurdu.
    server = FastMCP("trade-ai-readonly", streamable_http_path="/")
    for spec in specs_for_audience("mcp"):
        server.add_tool(
            _mcp_handler(spec), name=spec.name, description=spec.description
        )
    return server


class _BearerTokenMiddleware:
    """ASGI middleware: Authorization: Bearer <ADMIN_API_TOKEN> zorunlu."""

    def __init__(self, app: Any, expected_token: str) -> None:
        self._app = app
        self._expected = expected_token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error": "invalid MCP token"}',
                }
            )
            return
        await self._app(scope, receive, send)


def build_mcp_asgi_app() -> tuple[Any, Any]:
    """(ASGI app, session_manager) çifti döndür.

    Çağıran taraf (app/main.py) ASGI app'i ``/mcp``'ye mount eder ve
    session manager'ın ``run()`` context'ini lifespan içinde çalıştırır.
    """
    server = build_mcp_server()
    asgi_app = server.streamable_http_app()
    protected = _BearerTokenMiddleware(asgi_app, settings.effective_admin_api_token)
    return protected, server.session_manager
