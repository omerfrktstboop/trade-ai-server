"""Tests for app/core/logger.py — signal.log JSON-lines writer."""

from __future__ import annotations

import json
from datetime import datetime

from app.core.logger import log_signal_evaluation


def test_log_signal_evaluation_writes_istanbul_local_time(tmp_path, monkeypatch):
    """Timestamps are written in Europe/Istanbul local time (not UTC) so a
    human reading logs/signal.log sees the real wall-clock time, not a
    value that's 3 hours behind."""
    log_file = tmp_path / "signal.log"
    monkeypatch.setattr("app.core.logger.LOG_DIR", tmp_path)
    monkeypatch.setattr("app.core.logger.LOG_FILE", log_file)

    log_signal_evaluation(
        request_id="test-tz",
        symbol="THYAO",
        mode="PAPER",
        request={"symbol": "THYAO"},
        response={"action": "WAIT"},
    )

    line = log_file.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["requestId"] == "test-tz"

    ts = datetime.fromisoformat(entry["timestamp"])
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 3 * 3600  # Europe/Istanbul = UTC+3
