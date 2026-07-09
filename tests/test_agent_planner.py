"""Tests for agent_planner.plan_next() — symbol-allow gate config injection."""

from __future__ import annotations

from app.core.risk_config import RiskConfig
from app.models.signal import AgenticAction, AgenticDataType
from app.services.agent_planner import _DATA_PRIORITY, plan_next
from app.services.session_store import SessionState


def _cfg(allowed: str) -> RiskConfig:
    return RiskConfig(allowed_symbols=allowed, _env_file=None)


class TestPlanNextRiskConfigInjection:
    def test_default_static_config_rejects_unknown_symbol(self):
        session = SessionState(rootSymbol="XNEW")
        result = plan_next(session)
        assert result.action == AgenticAction.WAIT
        assert result.proceed_to_ai is False
        assert "not in the allowed list" in result.reason

    def test_custom_runtime_config_allows_symbol_not_in_static_default(self):
        """A symbol added via the admin panel (DB) but absent from the static
        .env-backed default must be accepted when a fresh RiskConfig is passed —
        this is the fix for the /evaluate-agent vs /evaluate inconsistency."""
        session = SessionState(rootSymbol="XNEW")
        runtime_cfg = _cfg("XNEW,THYAO")

        result = plan_next(session, runtime_cfg)

        assert not (
            result.action == AgenticAction.WAIT and not result.proceed_to_ai
        ), "symbol should not be rejected once the runtime config allows it"

    def test_custom_runtime_config_still_rejects_disallowed_symbol(self):
        session = SessionState(rootSymbol="ZZZZ")
        runtime_cfg = _cfg("THYAO,AKBNK")

        result = plan_next(session, runtime_cfg)

        assert result.action == AgenticAction.WAIT
        assert result.proceed_to_ai is False


class TestPlannerDataPriority:
    def test_only_matriks_supported_live_fetch_types_are_requested(self):
        assert [data_type for data_type, _reason in _DATA_PRIORITY] == [
            AgenticDataType.DEPTH,
            AgenticDataType.OHLCV,
            AgenticDataType.TECHNICAL,
        ]
