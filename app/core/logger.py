"""Signal request/response logging — JSON-lines format to logs/signal.log.

No token or sensitive field is ever written.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "signal.log"


def _ensure_log_dir() -> None:
    """Create the log directory if it does not exist."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_signal_evaluation(
    *,
    request_id: str,
    symbol: str,
    mode: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> None:
    """Write a single JSON-lines entry to the signal log.

    Every entry is a self-contained JSON object on its own line.
    """
    _ensure_log_dir()

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requestId": request_id,
        "symbol": symbol,
        "mode": mode,
        "request": request,
        "response": response,
    }

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to write signal log entry")
