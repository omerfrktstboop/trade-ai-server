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
from app.models.ai_decision_context import AiDecisionContext

logger = logging.getLogger(__name__)

# ── Shared constants ───────────────────────────────────────────────────────────

_WAIT_FALLBACK: dict[str, Any] = {
    "action": "WAIT",
    "confidence": 0.0,
    "reason": "Provider fallback — safe WAIT",
}

# ── Tool-calling sınırları (v2 Faz 2, ilke #4) ────────────────────────────────
# Bütçe toplam wall-clock süredir: LLM turları + tool çağrıları dahil.
MAX_TOOL_ROUNDS = 4
MAX_TOOL_EXECUTIONS = 6

_BUDGET_EXHAUSTED_NUDGE = (
    "Tool budget exhausted — return the final JSON decision now, "
    "using the data you already have."
)
_JSON_ONLY_NUDGE = (
    "Your previous response was not valid JSON. Return only the final JSON "
    "decision object now, with no analysis, markdown, or commentary."
)


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
        "opportunity_score": ("opportunity_score", "opportunityScore"),
        "target_allocation_pct": (
            "target_allocation_pct",
            "targetAllocationPct",
        ),
        "research_score": ("research_score", "researchScore"),
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


def _compact_context_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the sole provider input contract at the provider boundary."""
    return AiDecisionContext.model_validate(payload).model_dump(exclude_none=True)


def _round_prompt_numbers(value: Any) -> Any:
    """Remove meaningless float tails without changing market precision."""
    if isinstance(value, float):
        rounded = round(value, 4)
        return int(rounded) if rounded.is_integer() else rounded
    if isinstance(value, dict):
        return {key: _round_prompt_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_prompt_numbers(item) for item in value]
    return value


def _build_payload_str(payload: dict[str, Any]) -> str:
    """Serialize a validated compact decision context for the user message."""
    compact = _round_prompt_numbers(_compact_context_payload(payload))
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _log_token_usage(data: dict[str, Any], *, phase: str) -> None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    logger.info(
        "DeepSeek tokens phase=%s prompt=%s completion=%s total=%s",
        phase,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )


# ── Abstract base ─────────────────────────────────────────────────────────────


class AiProvider(ABC):
    """Interface every AI provider must implement."""

    #: True after enough consecutive decide() failures that new BUY signals
    #: should not be trusted (T8). Providers that cannot fail this way (e.g.
    #: MockAiProvider) leave this permanently False.
    is_degraded: bool = False
    consecutive_failures: int = 0

    @abstractmethod
    async def decide(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        veto_only: bool = False,
    ) -> dict[str, Any]:
        """Produce a trading decision from an ``AiDecisionContext`` payload.

        Parameters:
            payload: Serialized ``ai-decision-context-v2`` fields only.
            request_id: Evaluation request id, forwarded to tool-call audit
                rows so a tool invocation can be traced to its evaluation.

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

    async def decide(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        veto_only: bool = False,
    ) -> dict[str, Any]:
        _compact_context_payload(payload)
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
        max_attempts: int | None = None,
        degraded_threshold: int | None = None,
        probe_interval_seconds: float | None = None,
        tools_enabled: bool | None = None,
        tool_budget_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # v2 Faz 2: bayrak kapalıyken (default) tek-atış davranış birebir.
        self.tools_enabled = (
            tools_enabled if tools_enabled is not None else settings.ai_tools_enabled
        )
        self.tool_budget_seconds = float(
            tool_budget_seconds
            if tool_budget_seconds is not None
            else settings.deepseek_tool_budget_seconds
        )
        self.max_attempts = max(1, max_attempts or settings.deepseek_max_attempts)
        self.degraded_threshold = max(
            1, degraded_threshold or settings.ai_degraded_threshold
        )
        self.probe_interval_seconds = float(
            probe_interval_seconds
            if probe_interval_seconds is not None
            else settings.ai_degraded_probe_interval_seconds
        )
        # Consecutive-failure tracking for the "AI degraded" status (T8):
        # once >= degraded_threshold, decide() short-circuits to WAIT without
        # attempting a network call, except for one periodic probe attempt
        # every probe_interval_seconds so recovery can be detected.
        self.consecutive_failures = 0
        self.last_failure_at: float | None = None
        self.last_attempt_at: float | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @property
    def is_degraded(self) -> bool:
        return self.consecutive_failures >= self.degraded_threshold

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_at = time.monotonic()

    def _record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_failure_at = None

    async def decide(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        veto_only: bool = False,
    ) -> dict[str, Any]:
        """Send compact decision context to DeepSeek, parse JSON response.

        On any error (network, timeout, parse) → WAIT fallback. Network and
        timeout errors retry up to ``max_attempts`` times with exponential
        backoff; a persistent API/parse failure does not retry (retrying an
        auth error or a malformed-JSON response from the same prompt is very
        unlikely to help, and only adds latency to a scanner tick).

        When already degraded (consecutive_failures >= degraded_threshold),
        skips the network call entirely and returns WAIT immediately, except
        for one periodic probe attempt every probe_interval_seconds so
        recovery can be detected without hammering a down provider.
        """
        if self.is_degraded:
            now = time.monotonic()
            due_for_probe = (
                self.last_attempt_at is None
                or (now - self.last_attempt_at) >= self.probe_interval_seconds
            )
            if not due_for_probe:
                return {
                    **_WAIT_FALLBACK,
                    "reason": (
                        f"AI provider degraded ({self.consecutive_failures} "
                        "consecutive failures); skipping call until next probe"
                    ),
                }

        t0 = time.monotonic()
        self.last_attempt_at = t0

        # Veto akışı (Plan Faz 1.4) tool'suz çalışır: seviyeler zaten
        # deterministik, AI yalnızca BUY/WAIT onayı verir.
        if self.tools_enabled and not veto_only:
            # Fix #5: 12 sn'lik toplam bütçe LLM turları + tool çağrıları +
            # audit dahil KESİN bir asyncio.timeout ile uygulanır. Deadline
            # kontrolleri son turu erken zorlar; bu timeout ise takılan bir
            # ağ/tool çağrısında sert üst sınırdır.
            try:
                async with asyncio.timeout(self.tool_budget_seconds):
                    result = await self._decide_with_tools(payload, t0, request_id)
            except asyncio.TimeoutError:
                result = {
                    **_WAIT_FALLBACK,
                    "reason": (
                        f"Tool budget hard timeout after {self.tool_budget_seconds}s"
                    ),
                    "_transient_failure": True,
                }
        else:
            system_content = get_trading_system_prompt(veto_only=veto_only)
            user_content = _build_payload_str(payload)
            logger.info(
                "DeepSeek input size systemChars=%d contextChars=%d tools=false",
                len(system_content),
                len(user_content),
            )
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ]

            body = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.3,
                # 500 (was 300): the mandatory bear_case field for BUYs adds 1-2
                # sentences to the JSON output — a truncated response fails
                # _extract_json and falls back to WAIT, so leave headroom.
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
            }
            result = await self._call_with_retry(body, t0)

        if result.get("_transient_failure"):
            self._record_failure()
            result = {k: v for k, v in result.items() if k != "_transient_failure"}
        else:
            self._record_success()
        return result

    # ── Tool-calling döngüsü (v2 Faz 2) ───────────────────────────────────

    async def _decide_with_tools(
        self, payload: dict[str, Any], t0: float, request_id: str | None = None
    ) -> dict[str, Any]:
        """Sınırlı tool-calling döngüsü: en fazla MAX_TOOL_ROUNDS tur,
        MAX_TOOL_EXECUTIONS çağrı ve toplam tool_budget_seconds wall-clock
        (LLM turları dahil). Tool hataları döngüyü asla kırmaz; her hata
        modele ``{"error": ...}`` içeriği olarak döner. Her hata yolu WAIT
        fallback'ine iner — asla exception fırlatmaz."""
        # Lazy import: registry → (lazy) pipeline → ai_provider döngüsünü
        # module-import seviyesinde kurmamak için.
        from app.tools import call_tool, openai_tool_definitions

        symbol_scope = str(payload.get("symbol") or "").strip().upper()
        deadline = t0 + self.tool_budget_seconds
        tools = openai_tool_definitions("ai")
        tool_names_used: list[str] = []
        executions = 0
        force_json_final = False

        system_content = get_trading_system_prompt(tools_enabled=True)
        user_content = _build_payload_str(payload)
        logger.info(
            "DeepSeek input size systemChars=%d contextChars=%d tools=true toolCount=%d",
            len(system_content),
            len(user_content),
            len(tools),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

        for round_index in range(MAX_TOOL_ROUNDS + 1):
            force_final = (
                force_json_final
                or round_index >= MAX_TOOL_ROUNDS
                or executions >= MAX_TOOL_EXECUTIONS
                or time.monotonic() >= deadline
            )
            if force_final and round_index > 0 and not force_json_final:
                messages.append({"role": "user", "content": _BUDGET_EXHAUSTED_NUDGE})

            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
                "tools": tools,
                "tool_choice": "none" if force_final else "auto",
            }
            remaining = deadline - time.monotonic()
            per_call_timeout = min(self.timeout, max(2.0, remaining))
            message, failure_reason = await self._tool_round_completion(
                body, per_call_timeout
            )
            if message is None:
                return {
                    **_WAIT_FALLBACK,
                    "reason": failure_reason or "Unknown error",
                    "_transient_failure": True,
                }

            tool_calls = message.get("tool_calls") or []
            if tool_calls and not force_final:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    if executions >= MAX_TOOL_EXECUTIONS or time.monotonic() >= deadline:
                        result: dict[str, Any] = {
                            "tool": name,
                            "error": "tool budget exhausted",
                        }
                    else:
                        executions += 1
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                            if not isinstance(args, dict):
                                args = {}
                        except json.JSONDecodeError:
                            args = {}
                        result = await call_tool(
                            name,
                            args,
                            caller="deepseek",
                            request_id=request_id,
                            symbol_scope=symbol_scope or None,
                        )
                        tool_names_used.append(name)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(tc.get("id") or ""),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            content = message.get("content") or ""
            parsed = _extract_json(content) if content else None
            if parsed is None:
                if not force_final and round_index < MAX_TOOL_ROUNDS:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": _JSON_ONLY_NUDGE})
                    force_json_final = True
                    continue
                logger.warning(
                    "DeepSeek tool-loop JSON parse failed (round=%d). Raw (200): %s",
                    round_index,
                    str(content)[:200],
                )
                return {
                    **_WAIT_FALLBACK,
                    "reason": (
                        "Could not parse model response as JSON: "
                        f"{str(content)[:100]}..."
                    ),
                    "_transient_failure": True,
                }

            decision = _normalize_decision(parsed)
            decision["_response_time_ms"] = int((time.monotonic() - t0) * 1000)
            audit_raw = decision.get("_audit_raw_response")
            if isinstance(audit_raw, dict):
                audit_raw["toolCallsUsed"] = list(tool_names_used)
            logger.info(
                "DeepSeek tool-loop decision: action=%s confidence=%.1f "
                "rounds=%d toolCalls=%d elapsed_ms=%d",
                decision["action"],
                decision["confidence"],
                round_index + 1,
                len(tool_names_used),
                decision["_response_time_ms"],
            )
            return decision

        # force_final son turda kesin final ürettiği için buraya inilmez.
        return {  # pragma: no cover — savunma amaçlı
            **_WAIT_FALLBACK,
            "reason": "Tool loop exhausted without a final decision",
            "_transient_failure": True,
        }

    async def _tool_round_completion(
        self, body: dict[str, Any], timeout_seconds: float
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Tek LLM turu: assistant mesajını (content + tool_calls) ham döndür.

        12 sn'lik toplam bütçeye backoff'lu retry sığmadığı için tur başına
        tek deneme yapılır; hata (None, reason) olarak döner ve decide()
        seviyesinde WAIT fallback + degraded sayacına dönüşür."""
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "DeepSeek tool-round error %d: %s",
                            resp.status,
                            error_text[:500],
                        )
                        return None, f"API error {resp.status}"
                    data = await resp.json()
        except asyncio.TimeoutError:
            return None, f"Request timed out after {timeout_seconds:.0f}s"
        except aiohttp.ClientError as exc:
            return None, f"Network error: {exc}"
        except Exception as exc:  # noqa: BLE001 — her hata WAIT'e iner
            logger.exception("DeepSeek tool-round unexpected error")
            return None, f"Unexpected error: {exc}"

        _log_token_usage(data, phase="tool-round")
        choices = data.get("choices", [])
        if not choices:
            return None, "Empty response from model"
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            return None, "Malformed message from model"
        return message, None

    async def _call_with_retry(self, body: dict[str, Any], t0: float) -> dict[str, Any]:
        last_reason = "Unknown error"
        for attempt in range(1, self.max_attempts + 1):
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
                            "DeepSeek API: status=%d elapsed=%.2fs model=%s attempt=%d/%d",
                            resp.status,
                            elapsed,
                            self.model,
                            attempt,
                            self.max_attempts,
                        )

                        if resp.status >= 500:
                            error_text = await resp.text()
                            last_reason = f"API error {resp.status}"
                            logger.error(
                                "DeepSeek API error %d (attempt %d/%d): %s",
                                resp.status,
                                attempt,
                                self.max_attempts,
                                error_text[:500],
                            )
                            if attempt < self.max_attempts:
                                await asyncio.sleep(2 ** (attempt - 1))
                                continue
                            return {
                                **_WAIT_FALLBACK,
                                "reason": last_reason,
                                "_transient_failure": True,
                            }

                        if resp.status != 200:
                            error_text = await resp.text()
                            logger.error(
                                "DeepSeek API error %d: %s",
                                resp.status,
                                error_text[:500],
                            )
                            # 4xx (auth, bad request, ...) will not be fixed
                            # by retrying the identical request, but still
                            # counts toward the degraded-status threshold —
                            # every subsequent call will fail identically.
                            return {
                                **_WAIT_FALLBACK,
                                "reason": f"API error {resp.status}",
                                "_transient_failure": True,
                            }

                        data = await resp.json()
                        _log_token_usage(data, phase="decision")
                        break

            except aiohttp.ClientError as exc:
                elapsed = time.monotonic() - t0
                last_reason = f"Network error: {exc}"
                logger.error(
                    "DeepSeek network error after %.2fs (attempt %d/%d): %s",
                    elapsed,
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt < self.max_attempts:
                    await asyncio.sleep(2 ** (attempt - 1))
                    continue
                return {
                    **_WAIT_FALLBACK,
                    "reason": last_reason,
                    "_transient_failure": True,
                }

            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                last_reason = f"Request timed out after {self.timeout:.0f}s"
                logger.error(
                    "DeepSeek request timed out after %.2fs (attempt %d/%d)",
                    elapsed,
                    attempt,
                    self.max_attempts,
                )
                if attempt < self.max_attempts:
                    await asyncio.sleep(2 ** (attempt - 1))
                    continue
                return {
                    **_WAIT_FALLBACK,
                    "reason": last_reason,
                    "_transient_failure": True,
                }

            except Exception as exc:
                elapsed = time.monotonic() - t0
                logger.exception(
                    "DeepSeek unexpected error after %.2fs (attempt %d/%d)",
                    elapsed,
                    attempt,
                    self.max_attempts,
                )
                return {
                    **_WAIT_FALLBACK,
                    "reason": f"Unexpected error: {exc}",
                    "_transient_failure": True,
                }
        else:
            return {**_WAIT_FALLBACK, "reason": last_reason, "_transient_failure": True}

        # Extract assistant message content
        choices = data.get("choices", [])
        if not choices:
            logger.warning("DeepSeek returned empty choices")
            return {
                **_WAIT_FALLBACK,
                "reason": "Empty response from model",
                "_transient_failure": True,
            }

        content = choices[0].get("message", {}).get("content", "")

        if not content:
            logger.warning("DeepSeek returned empty content")
            return {
                **_WAIT_FALLBACK,
                "reason": "Empty content from model",
                "_transient_failure": True,
            }

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
                "_transient_failure": True,
            }

        decision = _normalize_decision(parsed)
        # Persisted to ai_decisions.response_time_ms; underscore prefix keeps
        # it out of the trading schema like _audit_raw_response.
        decision["_response_time_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info(
            "DeepSeek decision: action=%s confidence=%.1f elapsed_ms=%d",
            decision["action"],
            decision["confidence"],
            decision["_response_time_ms"],
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
            tools_enabled=settings.ai_tools_enabled,
            tool_budget_seconds=settings.deepseek_tool_budget_seconds,
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


def get_ai_provider_status() -> dict[str, Any]:
    """AI health summary for the admin panel (T8/T9)."""
    provider = get_default_provider()
    return {
        "providerName": type(provider).__name__,
        "isDegraded": provider.is_degraded,
        "consecutiveFailures": provider.consecutive_failures,
        "toolCallingEnabled": bool(getattr(provider, "tools_enabled", False)),
    }
