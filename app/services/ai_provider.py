"""AI provider abstraction layer.

Each provider implements ``async def decide(payload) -> dict`` and returns
a JSON-serializable decision dictionary. The provider factory selects the
active implementation based on ``AI_PROVIDER`` in the app config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from app.config import AIProvider, settings
from app.core.prompts import get_trading_system_prompt

logger = logging.getLogger(__name__)

# ── Shared constants ───────────────────────────────────────────────────────────

_WAIT_FALLBACK: dict[str, Any] = {
    "action": "WAIT",
    "confidence": 0.0,
    "reason": "Provider fallback — safe WAIT",
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from a model response.

    Tries multiple strategies in order:
    1. Direct JSON.parse
    2. Extract from ```json … ```
    3. Extract from ``` … ```
    4. Find first { … } pair
    """
    text = text.strip()

    # Strategy 1 — direct parse
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    # Strategy 2 — ```json block
    if "```json" in text:
        try:
            # Find the first ```json and extract until closing ```
            start = text.index("```json") + 7
            end = text.index("```", start)
            return json.loads(text[start:end].strip())  # type: ignore[no-any-return]
        except (ValueError, json.JSONDecodeError):
            pass

    # Strategy 3 — ``` block (no language tag)
    if "```" in text:
        try:
            start = text.index("```") + 3
            end = text.index("```", start)
            return json.loads(text[start:end].strip())  # type: ignore[no-any-return]
        except (ValueError, json.JSONDecodeError):
            pass

    # Strategy 4 — find outermost { … }
    try:
        brace_start = text.index("{")
        brace_end = text.rindex("}") + 1
        return json.loads(text[brace_start:brace_end])  # type: ignore[no-any-return]
    except (ValueError, json.JSONDecodeError):
        pass

    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Public wrapper around :func:`_extract_json` for non-trading callers.

    Kept as a thin alias rather than renaming ``_extract_json`` outright —
    the trading-decision code path and its tests reference the private name
    directly; this gives other services (e.g. the weekly review agent) the
    same battle-tested JSON extraction without importing a "private" symbol.
    """
    return _extract_json(text)


def _normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure the decision dict has the required fields with valid values."""
    action = str(raw.get("action", "WAIT")).upper()
    if action not in ("BUY", "SELL", "WAIT"):
        action = "WAIT"

    try:
        confidence = float(raw.get("confidence", 50.0))
    except (TypeError, ValueError):
        confidence = 50.0
    confidence = max(0.0, min(100.0, confidence))

    reason = str(raw.get("reason", "No reason provided"))

    result: dict[str, Any] = {
        "action": action,
        "confidence": confidence,
        "reason": reason,
        "_audit_raw_response": raw,
    }

    # Optional numeric fields — accept camelCase (matches the rest of the API's
    # JSON convention) as well as the snake_case documented in the system prompt.
    numeric_aliases: dict[str, tuple[str, ...]] = {
        "stop_loss": ("stop_loss", "stopLoss"),
        "target_price": ("target_price", "targetPrice"),
        "risk_score": ("risk_score", "riskScore"),
    }
    for dest_field, aliases in numeric_aliases.items():
        for alias in aliases:
            if alias not in raw:
                continue
            try:
                result[dest_field] = float(raw[alias])
                break
            except (TypeError, ValueError):
                continue

    # entry_range — pass through untouched (nested {min,max}/{entryMin,entryMax}
    # or flat entryMin/entryMax, either casing); _parse_entry_range() downstream
    # already understands every shape/casing combination.
    for field in (
        "entry_range",
        "entryRange",
        "entry_min",
        "entryMin",
        "entry_max",
        "entryMax",
    ):
        if field in raw:
            result[field] = raw[field]

    # bear_case — informational only (persisted in raw_response, shown in the
    # admin log detail view); never feeds RiskEngine. Without this pass-through
    # the whitelist above would silently drop it.
    for alias in ("bear_case", "bearCase"):
        if alias in raw and raw[alias] is not None:
            result["bear_case"] = str(raw[alias])
            break

    return result


def _build_payload_str(payload: dict[str, Any]) -> str:
    """Serialize payload to a compact JSON string for the user message."""
    # Filter out large/token fields to keep prompt concise
    relevant = {k: v for k, v in payload.items() if k != "requestId"}
    return json.dumps(relevant, indent=2, ensure_ascii=False, default=str)


# ── Abstract base ─────────────────────────────────────────────────────────────


class AiProvider(ABC):
    """Interface every AI provider must implement."""

    @abstractmethod
    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Produce a trading decision from the signal payload.

        Parameters:
            payload: Serialized SignalRequest fields (OHLC, indicators, …).

        Returns:
            A dict with at minimum:
            - ``action``: ``"BUY"``, ``"SELL"``, or ``"WAIT"``
            - ``confidence``: float 0-100
            - ``reason``: human-readable explanation
            Optional: ``stop_loss``, ``target_price``, ``risk_score``
        """
        ...

    @abstractmethod
    async def chat(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 800
    ) -> str:
        """Generic chat completion — raw text response, no trading schema.

        Used by non-trading LLM tasks (e.g. the weekly self-reflection
        review) that need a custom system prompt instead of the hardcoded
        trading one. Never raises — any network/API/parse failure returns
        ``""`` so callers can degrade gracefully (e.g. skip persisting a
        lesson rather than crash a scheduled job).
        """
        ...


# ── Mock provider ─────────────────────────────────────────────────────────────


class MockAiProvider(AiProvider):
    """Always returns WAIT — safe no-op for testing and development."""

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        logger.debug("MockAiProvider.decide called — returning WAIT")
        return {
            "action": "WAIT",
            "confidence": 0.0,
            "reason": "Mock provider — always WAIT",
        }

    async def chat(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 800
    ) -> str:
        logger.debug("MockAiProvider.chat called — returning empty string")
        return ""


# ── DeepSeek provider ─────────────────────────────────────────────────────────


class DeepSeekProvider(AiProvider):
    """DeepSeek API provider via OpenAI-compatible chat completions.

    Config (env vars)::

        DEEPSEEK_API_KEY=sk-…
        DEEPSEEK_MODEL=deepseek-chat
        DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
        DEEPSEEK_TIMEOUT=30
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send signal payload to DeepSeek, parse JSON response.

        On any error (network, timeout, parse) → WAIT fallback.
        """
        messages = [
            {"role": "system", "content": get_trading_system_prompt()},
            {"role": "user", "content": _build_payload_str(payload)},
        ]

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            # 500 (was 300): the mandatory bear_case field for BUYs adds 1-2
            # sentences to the JSON output — a truncated response fails
            # _extract_json and falls back to WAIT, so leave headroom.
            "max_tokens": 500,
        }

        t0 = time.monotonic()

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    elapsed = time.monotonic() - t0
                    logger.info(
                        "DeepSeek API: status=%d elapsed=%.2fs model=%s",
                        resp.status,
                        elapsed,
                        self.model,
                    )

                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "DeepSeek API error %d: %s",
                            resp.status,
                            error_text[:500],
                        )
                        return {**_WAIT_FALLBACK, "reason": f"API error {resp.status}"}

                    data = await resp.json()

        except aiohttp.ClientError as exc:
            elapsed = time.monotonic() - t0
            logger.error("DeepSeek network error after %.2fs: %s", elapsed, exc)
            return {**_WAIT_FALLBACK, "reason": f"Network error: {exc}"}

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error("DeepSeek request timed out after %.2fs", elapsed)
            return {
                **_WAIT_FALLBACK,
                "reason": f"Request timed out after {self.timeout:.0f}s",
            }

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("DeepSeek unexpected error after %.2fs", elapsed)
            return {**_WAIT_FALLBACK, "reason": f"Unexpected error: {exc}"}

        # Extract assistant message content
        choices = data.get("choices", [])
        if not choices:
            logger.warning("DeepSeek returned empty choices")
            return {**_WAIT_FALLBACK, "reason": "Empty response from model"}

        content = choices[0].get("message", {}).get("content", "")

        if not content:
            logger.warning("DeepSeek returned empty content")
            return {**_WAIT_FALLBACK, "reason": "Empty content from model"}

        # Parse JSON from content
        parsed = _extract_json(content)

        if parsed is None:
            logger.warning(
                "DeepSeek JSON parse failed. Raw content (200 chars): %s",
                content[:200],
            )
            return {
                **_WAIT_FALLBACK,
                "reason": f"Could not parse model response as JSON: {content[:100]}...",
            }

        decision = _normalize_decision(parsed)
        logger.info(
            "DeepSeek decision: action=%s confidence=%.1f",
            decision["action"],
            decision["confidence"],
        )
        return decision

    async def chat(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 800
    ) -> str:
        """Generic chat completion for non-trading tasks — raw text out.

        Deliberately independent of :meth:`decide` (small duplication of the
        HTTP call) rather than a shared refactor: ``decide()``'s error-path
        reason strings are asserted verbatim by the trading-decision test
        suite, and this keeps that path untouched.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }

        t0 = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    elapsed = time.monotonic() - t0
                    logger.info(
                        "DeepSeek chat: status=%d elapsed=%.2fs model=%s",
                        resp.status,
                        elapsed,
                        self.model,
                    )
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "DeepSeek chat error %d: %s", resp.status, error_text[:500]
                        )
                        return ""
                    data = await resp.json()
        except Exception as exc:  # noqa: BLE001 — any failure degrades to "" here
            logger.error(
                "DeepSeek chat failed after %.2fs: %s", time.monotonic() - t0, exc
            )
            return ""

        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", "") or "")


# ── Provider factory ──────────────────────────────────────────────────────────


def get_provider(name: str | AIProvider | None = None) -> AiProvider:
    """Return a configured provider instance for the given name.

    Parameters:
        name: One of ``"mock"``, ``"deepseek"``, or None (uses ``settings.ai_provider``).

    Returns:
        An AiProvider instance ready for ``await provider.decide(payload)``.

    Raises:
        ValueError: Unknown provider name.
    """
    resolved = None
    if isinstance(name, AIProvider):
        resolved = name.value
    elif isinstance(name, str):
        resolved = name.lower()
    else:
        resolved = settings.ai_provider.value

    if resolved in ("mock",):
        return MockAiProvider()

    if resolved in ("deepseek",):
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout,
        )

    raise ValueError(f"Unknown AI_PROVIDER: {resolved!r}. Supported: mock, deepseek")


# ── Module-level default ──────────────────────────────────────────────────────

_default_provider: AiProvider | None = None


def get_default_provider() -> AiProvider:
    """Return the singleton default provider (lazy-init from settings)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = get_provider()
    return _default_provider
