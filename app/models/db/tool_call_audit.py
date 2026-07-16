"""Audit log for read-only tool invocations (AI tool-calling + MCP surface)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ToolCallAudit(Base):
    __tablename__ = "tool_call_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    caller: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    symbol_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    args_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
