"""Application file logging in JSON-lines format.

No token or sensitive field is ever written.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "signal.log"
MATRIKS_LOG_FILE = LOG_DIR / "matriks.log"
RUNTIME_EVENTS_LOG_FILE = LOG_DIR / "runtime_events.log"

# Timestamps are written in Europe/Istanbul local time (with an explicit
# offset, so still unambiguous/machine-parseable) rather than UTC, since
# this file exists for humans to read while debugging — a raw UTC stamp
# reads 3 hours behind the wall clock for a BIST-only deployment.
_LOG_TZ = ZoneInfo("Europe/Istanbul")


def _ensure_log_dir() -> None:
    """Create the log directory if it does not exist."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _append_json_line(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_signal_evaluation(
    *,
    request_id: str,
    symbol: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> None:
    """Write a single JSON-lines entry to the signal log.

    Every entry is a self-contained JSON object on its own line.
    """
    _ensure_log_dir()

    entry = {
        "timestamp": datetime.now(_LOG_TZ).isoformat(),
        "requestId": request_id,
        "symbol": symbol,
        "request": request,
        "response": response,
    }

    try:
        _append_json_line(LOG_FILE, entry)
    except OSError:
        logger.exception("Failed to write signal log entry")


def log_matriks_gateway(
    *,
    gateway_timestamp: datetime,
    level: str,
    message: str,
) -> None:
    """Append one authenticated Matriks gateway event to ``matriks.log``."""
    entry = {
        "timestamp": gateway_timestamp.isoformat(),
        "receivedAt": datetime.now(_LOG_TZ).isoformat(),
        "level": level,
        "source": "TradeAiGateway",
        "message": message,
    }
    _append_json_line(MATRIKS_LOG_FILE, entry)


def log_runtime_event(*, event_type: str, detail: str) -> None:
    """Append a durable record of a safety-relevant in-memory runtime event.

    Flags such as the startup dispatch hard-block (``app.core.runtime_flags``)
    only ever live in process memory and are otherwise visible solely in
    console stdout, which is not reliably captured across deploys — this is
    the only durable trace of when/why they fired.
    """
    entry = {
        "timestamp": datetime.now(_LOG_TZ).isoformat(),
        "eventType": event_type,
        "detail": detail,
    }
    try:
        _append_json_line(RUNTIME_EVENTS_LOG_FILE, entry)
    except OSError:
        logger.exception("Failed to write runtime event log entry")
