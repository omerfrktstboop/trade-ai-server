"""Application file logging in JSON-lines format.

No token or sensitive field is ever written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "signal.log"
MATRIKS_LOG_FILE = LOG_DIR / "matriks.log"
RUNTIME_EVENTS_LOG_FILE = LOG_DIR / "runtime_events.log"
APP_LOG_FILE = LOG_DIR / "app.log"

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


async def log_signal_evaluation(
    *,
    request_id: str,
    symbol: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> None:
    """Write a single JSON-lines entry to the signal log.

    Every entry is a self-contained JSON object on its own line. The actual
    write runs off the event loop (see ``_append_json_line`` callers below) —
    calling this from a hot async path must never block other requests on
    disk I/O; that starvation is what caused the connection pile-up this
    fixes (see ``configure_file_logging``'s docstring for the full story).
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
        await asyncio.to_thread(_append_json_line, LOG_FILE, entry)
    except OSError:
        logger.exception("Failed to write signal log entry")


async def log_matriks_gateway(
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
    await asyncio.to_thread(_append_json_line, MATRIKS_LOG_FILE, entry)


async def log_runtime_event(*, event_type: str, detail: str) -> None:
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
        await asyncio.to_thread(_append_json_line, RUNTIME_EVENTS_LOG_FILE, entry)
    except OSError:
        logger.exception("Failed to write runtime event log entry")


_file_logging_configured = False
_queue_listener: logging.handlers.QueueListener | None = None


def configure_file_logging() -> None:
    """Attach a rotating file handler to the root logger, once — off-thread.

    Every ``logger.warning``/``logger.info`` call across the app (order
    dispatch gates, preflight rejections, account watcher events, ...)
    otherwise only reaches the console. On this deployment console output
    is redirected to a fixed-path file that gets overwritten on every
    process restart (see ``Start-TradeAiServer.ps1``), so any decision
    made between two restarts is unrecoverable once the next restart
    happens — which is routine during active deploys. A rotating file
    that the app itself appends to survives restarts.

    A plain ``RotatingFileHandler`` on the root logger writes synchronously
    on whatever thread calls ``logger.info(...)`` — on this app's single
    asyncio event loop, that means every log call blocks request handling
    for the duration of a disk write. Matriks forwards each per-symbol
    market-data warning as its own HTTP call (``/api/gateway-log``), and its
    60s rate-limit window resets for every subscribed symbol at close to the
    same time, so the gateway can burst dozens of near-simultaneous requests;
    with a blocking handler that burst froze the event loop long enough for
    inbound TCP connections to pile up in CloseWait until the server stopped
    accepting new ones. Routing through ``QueueHandler``/``QueueListener``
    makes ``logger.*()`` calls a fast in-memory enqueue — the actual file
    write happens on a dedicated listener thread, off the event loop.
    """
    global _file_logging_configured, _queue_listener
    if _file_logging_configured:
        return
    _ensure_log_dir()
    file_handler = logging.handlers.RotatingFileHandler(
        APP_LOG_FILE, maxBytes=20_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    file_handler.setLevel(logging.INFO)

    log_queue: Queue = Queue(-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(queue_handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    _queue_listener = logging.handlers.QueueListener(
        log_queue, file_handler, respect_handler_level=True
    )
    _queue_listener.start()
    _file_logging_configured = True
