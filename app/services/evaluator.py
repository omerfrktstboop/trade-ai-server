"""Backward-compatible re-export shim.

The evaluator was split into ``app.services.evaluation`` (parsing.py,
persistence.py, payload.py, pipeline.py) for maintainability. Import from
``app.services.evaluation`` in new code; this module exists so existing
``from app.services.evaluator import ...`` call sites keep working.
"""

from __future__ import annotations

from app.config import settings  # noqa: F401
from app.services.evaluation import (  # noqa: F401
    RELATED_SYMBOLS,
    EvaluationResult,
    _bot_average_cost_from_fill_ledger,
    _build_depth_context,
    _build_position_context,
    _build_request_id,
    _build_technical_feature_payload,
    _decision_persistence_metadata,
    _has_explicit_daily_trade_count,
    _json_safe,
    _log_evaluation,
    _parse_entry_range,
    _payload_get,
    _safe_action,
    _safe_decimal,
    _safe_float,
    _snapshot_step,
    _static_effective_config,
    _static_risk_engine,
    build_ai_decision_context,
    build_payload,
    dict_to_risk_decision,
    evaluate_symbol,
    kill_switch_response,
    persist_evaluation,
    persist_sizing_audit,
    snapshot_to_signal_request,
    with_fresh_account_sizing_context,
    with_resolved_daily_trade_count,
    with_runtime_controls,
    with_trade_eligibility,
)
