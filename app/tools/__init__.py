"""Read-only tool registry paketi (v2).

``import app.tools`` registry'yi VE katalog tanımlarını yükler — tüketiciler
(`ai_provider`, MCP app, testler) bu paketi import ettiğinde whitelist hazır
olur.
"""

from app.tools import catalog  # noqa: F401 — araçları registry'ye kaydeder
from app.tools.registry import (  # noqa: F401
    REGISTRY,
    ToolSpec,
    call_tool,
    specs_for_audience,
    tool,
)
from app.tools.openai_format import openai_tool_definitions  # noqa: F401
