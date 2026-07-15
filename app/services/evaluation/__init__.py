"""In-process signal evaluator for the full-inversion architecture.

Split into four modules:
- parsing.py: dependency-free raw-AI-output parsers (_safe_action/_safe_float/_safe_decimal).
- persistence.py: RiskDecision construction + AiDecision/RiskDecision/PositionSizingAudit writes.
- payload.py: SignalRequest/AI-payload context builders, including snapshot_to_signal_request.
- pipeline.py: evaluate_symbol and the runtime-control steps that wrap it.

``app.services.evaluator`` re-exports everything below for backward
compatibility with existing imports.
"""

from __future__ import annotations

from app.services.evaluation.parsing import (
    _safe_action,
    _safe_decimal,
    _safe_float,
)
from app.services.evaluation.persistence import (
    _decision_persistence_metadata,
    _json_safe,
    _parse_entry_range,
    dict_to_risk_decision,
    persist_evaluation,
    persist_sizing_audit,
)
from app.services.evaluation.payload import (
    _build_depth_context,
    _build_position_context,
    _build_request_id,
    _build_technical_feature_payload,
    _bot_average_cost_from_fill_ledger,
    _payload_get,
    _snapshot_step,
    build_ai_decision_context,
    build_payload,
    snapshot_to_signal_request,
)
from app.services.evaluation.pipeline import (
    RELATED_SYMBOLS,
    EvaluationResult,
    _has_explicit_daily_trade_count,
    _log_evaluation,
    _static_effective_config,
    _static_risk_engine,
    evaluate_symbol,
    kill_switch_response,
    with_fresh_account_sizing_context,
    with_resolved_daily_trade_count,
    with_runtime_controls,
    with_trade_eligibility,
)

__all__ = [
    "RELATED_SYMBOLS",
    "EvaluationResult",
    "_bot_average_cost_from_fill_ledger",
    "_build_depth_context",
    "_build_position_context",
    "_build_request_id",
    "_build_technical_feature_payload",
    "_decision_persistence_metadata",
    "_has_explicit_daily_trade_count",
    "_json_safe",
    "_log_evaluation",
    "_parse_entry_range",
    "_payload_get",
    "_safe_action",
    "_safe_decimal",
    "_safe_float",
    "_snapshot_step",
    "_static_effective_config",
    "_static_risk_engine",
    "build_ai_decision_context",
    "build_payload",
    "dict_to_risk_decision",
    "evaluate_symbol",
    "kill_switch_response",
    "persist_evaluation",
    "persist_sizing_audit",
    "snapshot_to_signal_request",
    "with_fresh_account_sizing_context",
    "with_resolved_daily_trade_count",
    "with_runtime_controls",
    "with_trade_eligibility",
]
