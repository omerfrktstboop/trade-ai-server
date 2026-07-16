"""Read-only tool registry — v2 mimarisinin tek araç tanım noktası.

Buradaki araçlar iki tüketiciye aynı tanımdan servis edilir:

- **DeepSeek function-calling** (``caller="deepseek"``): sadece
  ``audience``'ında ``"ai"`` olan araçlar; ``symbol`` parametresi alan
  araçlarda sembol kapsamı sunucu tarafında zorlanır (prompt'a güvenilmez).
- **MCP server** (``caller="mcp"``): ``audience``'ında ``"mcp"`` olan
  araçlar; admin token'ı arkasında dışa açılır.

Güvenlik sözleşmesi: registry'de YALNIZCA read-only araç tanımlanır. Emir
gönderme/iptal, config yazma, kill switch gibi yan etkili hiçbir yetenek bu
modül üzerinden erişilebilir olamaz — ``call_tool`` da hiçbir koşulda
exception fırlatmaz, hata her zaman ``{"error": ...}`` sözlüğü olarak döner.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError, create_model

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RESULT_CHARS = 16_000

#: Sembol kapsamı ihlali / audience reddi gibi güvenlik hataları için sabit
#: önekler — testler ve audit sorguları bunlara güvenir.
ERROR_UNKNOWN_TOOL = "unknown tool"
ERROR_AUDIENCE = "tool not available to this caller"
ERROR_SYMBOL_SCOPE = "symbol out of evaluation scope"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    audience: frozenset[str] = field(default_factory=lambda: frozenset({"ai", "mcp"}))


REGISTRY: dict[str, ToolSpec] = {}


def _params_model_from_signature(name: str, fn: Callable[..., Any]) -> type[BaseModel]:
    """Fonksiyon imzasından pydantic parametre modeli üret.

    Tip ipuçları JSON şemaya dönüşür; default'u olmayan parametreler zorunlu
    sayılır. Aynı model hem OpenAI function-calling şemasını hem FastMCP
    şemasını besler.
    """
    fields: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = (
            param.annotation if param.annotation is not inspect.Parameter.empty else Any
        )
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[param.name] = (annotation, default)
    return create_model(f"{name}_params", **fields)


def tool(
    name: str,
    description: str,
    *,
    audience: frozenset[str] | set[str] = frozenset({"ai", "mcp"}),
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Async bir fonksiyonu registry'ye read-only araç olarak kaydet."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if name in REGISTRY:
            raise ValueError(f"Tool already registered: {name}")
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            params_model=_params_model_from_signature(name, fn),
            handler=fn,
            timeout_seconds=timeout_seconds,
            max_result_chars=max_result_chars,
            audience=frozenset(audience),
        )
        return fn

    return decorator


def _allowed_symbols_for_scope(symbol_scope: str) -> set[str]:
    """Değerlendirilen sembol + RELATED_SYMBOLS bağlı sembolleri.

    Import döngüsünü kırmak için lazy import (pipeline → ai_provider →
    tools → pipeline zinciri oluşmasın).
    """
    allowed = {symbol_scope.upper()}
    try:
        from app.services.evaluation.pipeline import RELATED_SYMBOLS

        related = RELATED_SYMBOLS.get(symbol_scope.upper())
        if related:
            allowed.add(related.upper())
    except Exception:  # pragma: no cover — pipeline import edilemezse kapsam daralır
        logger.exception("RELATED_SYMBOLS load failed; scope stays single-symbol")
    return allowed


def _serialize_result(result: Any, max_chars: int) -> tuple[str, bool]:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


async def _write_audit(
    *,
    tool_name: str,
    caller: str,
    symbol_scope: str | None,
    args: dict[str, Any],
    ok: bool,
    error: str | None,
    result_chars: int,
    latency_ms: int,
    request_id: str | None,
) -> None:
    """Audit satırı yaz — hatası tool çağrısını asla düşürmez."""
    try:
        from app.db.session import async_session_factory
        from app.models.db.tool_call_audit import ToolCallAudit

        async with async_session_factory() as session:
            session.add(
                ToolCallAudit(
                    tool_name=tool_name,
                    caller=caller,
                    symbol_scope=symbol_scope,
                    args_json=json.dumps(args, ensure_ascii=False, default=str),
                    ok=ok,
                    error=error,
                    result_chars=result_chars,
                    latency_ms=latency_ms,
                    request_id=request_id,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Tool call audit write failed tool=%s", tool_name)


async def call_tool(
    name: str,
    args: dict[str, Any] | None,
    *,
    caller: str,
    request_id: str | None = None,
    symbol_scope: str | None = None,
) -> dict[str, Any]:
    """Bir aracı güvenlik kontrolleri + timeout + audit ile çalıştır.

    ASLA exception fırlatmaz; her sonuç ``{"tool": name, ...}`` sözlüğüdür —
    başarıda ``result`` (+ gerekirse ``truncated``), hatada ``error``.
    """
    args = dict(args or {})
    started = time.monotonic()

    async def _finish(
        payload: dict[str, Any], *, ok: bool, error: str | None, result_chars: int = 0
    ) -> dict[str, Any]:
        await _write_audit(
            tool_name=name,
            caller=caller,
            symbol_scope=symbol_scope,
            args=args,
            ok=ok,
            error=error,
            result_chars=result_chars,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        return payload

    spec = REGISTRY.get(name)
    if spec is None:
        msg = f"{ERROR_UNKNOWN_TOOL}: {name}"
        return await _finish({"tool": name, "error": msg}, ok=False, error=msg)

    if caller == "deepseek":
        if "ai" not in spec.audience:
            msg = f"{ERROR_AUDIENCE}: {name} (caller={caller})"
            return await _finish({"tool": name, "error": msg}, ok=False, error=msg)
        if "symbol" in spec.params_model.model_fields:
            requested = str(args.get("symbol", "")).strip().upper()
            if not symbol_scope or requested not in _allowed_symbols_for_scope(
                symbol_scope
            ):
                msg = (
                    f"{ERROR_SYMBOL_SCOPE}: requested={requested or '<empty>'} "
                    f"scope={symbol_scope or '<none>'}"
                )
                return await _finish({"tool": name, "error": msg}, ok=False, error=msg)

    try:
        validated = spec.params_model.model_validate(args)
    except ValidationError as exc:
        msg = f"invalid arguments: {exc.errors(include_url=False)}"
        return await _finish({"tool": name, "error": msg}, ok=False, error=msg)

    try:
        result = await asyncio.wait_for(
            spec.handler(**validated.model_dump()), timeout=spec.timeout_seconds
        )
    except asyncio.TimeoutError:
        msg = f"tool timed out after {spec.timeout_seconds}s"
        return await _finish({"tool": name, "error": msg}, ok=False, error=msg)
    except Exception as exc:
        msg = f"tool failed: {exc}"
        return await _finish({"tool": name, "error": msg}, ok=False, error=msg)

    text, truncated = _serialize_result(result, spec.max_result_chars)
    payload: dict[str, Any] = {"tool": name, "result": json.loads(text) if not truncated else text}
    if truncated:
        payload["truncated"] = True
    return await _finish(payload, ok=True, error=None, result_chars=len(text))


def specs_for_audience(audience: str) -> list[ToolSpec]:
    """Belirli bir tüketiciye açık araç tanımları (isme göre sıralı)."""
    return sorted(
        (spec for spec in REGISTRY.values() if audience in spec.audience),
        key=lambda spec: spec.name,
    )
