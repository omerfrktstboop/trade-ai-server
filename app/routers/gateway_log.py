"""Authenticated Matriks IQ gateway log ingestion."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.auth import verify_gateway_token
from app.core.logger import log_matriks_gateway

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gateway"], dependencies=[Depends(verify_gateway_token)])


class GatewayLogRequest(BaseModel):
    timestamp: datetime
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    message: str = Field(min_length=1, max_length=4000)


@router.post("/gateway-log", status_code=status.HTTP_202_ACCEPTED)
async def record_gateway_log(body: GatewayLogRequest) -> dict[str, str]:
    """Persist a gateway diagnostic without affecting trading control flow."""
    try:
        log_matriks_gateway(
            gateway_timestamp=body.timestamp,
            level=body.level,
            message=body.message,
        )
    except OSError as exc:
        logger.exception("Failed to persist Matriks gateway log")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway log persistence unavailable",
        ) from exc
    return {"status": "accepted"}
