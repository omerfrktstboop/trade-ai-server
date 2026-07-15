"""Creates the idempotent DecisionOutcome row for every AI evaluation - BUY,
SELL, WAIT, and blocked research candidates alike, not only executed orders
(Task 3.2). Called once per persisted evaluation from persist_evaluation();
never creates more than one row per request_id and never blocks or fails the
evaluation it is measuring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import DecisionOutcome, ResearchCandidate
from app.models.signal import SignalRequest, SignalResponse
from app.services.fill_ledger import to_decimal
from app.services.strategy_provenance import PROMPT_VERSION, STRATEGY_VERSION

logger = logging.getLogger(__name__)


async def create_decision_outcome(
    session: AsyncSession,
    req: SignalRequest,
    payload: dict[str, Any],
    raw_ai: dict[str, Any],
    response: SignalResponse,
) -> None:
    """Idempotently insert one PENDING DecisionOutcome row.

    Swallows all errors - outcome tracking is measurement-only and must
    never affect the evaluation it observes.
    """
    try:
        discovery_sources = None
        trend_pre_score = None
        candidate = (
            await session.execute(
                select(ResearchCandidate).where(
                    ResearchCandidate.symbol == req.symbol.strip().upper()
                )
            )
        ).scalar_one_or_none()
        if candidate is not None:
            discovery_sources = list(candidate.source) if candidate.source else None
            trend_pre_score = to_decimal(candidate.trend_pre_score)

        research_score = None
        if "research_score" in raw_ai:
            research_score = to_decimal(raw_ai.get("research_score"))

        values = dict(
            request_id=req.request_id,
            symbol=req.symbol.strip().upper(),
            evaluation_purpose=str(req.evaluation_purpose or "TRADING"),
            decision_action=response.action.value,
            decision_price=to_decimal(req.last_price),
            decision_at=datetime.now(timezone.utc),
            strategy_version=STRATEGY_VERSION,
            prompt_version=PROMPT_VERSION,
            profile_code=payload.get("profileCode"),
            config_hash=payload.get("configHash"),
            discovery_sources=discovery_sources,
            market_regime=req.market_regime,
            trend_pre_score=trend_pre_score,
            research_score=research_score,
            confidence_score=to_decimal(response.confidence_score),
            risk_score=to_decimal(response.risk_score),
            entry_price=to_decimal(response.price),
            stop_loss=to_decimal(response.stop_loss),
            target_price=to_decimal(response.target_price),
            outcome_status="PENDING",
        )
        dialect = session.bind.dialect.name
        statement = (
            (pg_insert(DecisionOutcome) if dialect == "postgresql" else sqlite_insert(DecisionOutcome))
            .values(**values)
            .on_conflict_do_nothing(index_elements=["request_id"])
        )
        await session.execute(statement)
    except Exception:
        logger.exception(
            "DECISION_OUTCOME_CREATE_FAILED request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
