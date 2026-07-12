"""Best-effort Telegram notifications for operational events.

Notifications must never affect the scanner, order path, or HTTP responses.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
_THROTTLE = timedelta(minutes=5)


class NotificationService:
    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token if token is not None else settings.telegram_bot_token
        self._chat_id = chat_id if chat_id is not None else settings.telegram_chat_id
        self._transport = transport
        self._last_sent: dict[str, datetime] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(
        self,
        level: str,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        event_key: str | None = None,
    ) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled; notification skipped: %s", message)
            return False

        key = event_key or f"{level}:{message}"
        now = datetime.now(timezone.utc)
        previous = self._last_sent.get(key)
        if previous is not None and now - previous < _THROTTLE:
            logger.debug("Telegram notification throttled key=%s", key)
            return False
        self._last_sent[key] = now

        text = _format_message(level, message, context)
        try:
            async with httpx.AsyncClient(
                timeout=5.0, transport=self._transport
            ) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat_id, "text": text},
                )
                response.raise_for_status()
            return True
        except Exception:
            logger.exception("Telegram notification failed key=%s", key)
            return False


def _format_message(level: str, message: str, context: dict[str, Any] | None) -> str:
    lines = [f"Trade AI [{level.upper()}]", message]
    for key, value in (context or {}).items():
        if value is not None and value != "":
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


notification_service = NotificationService()


async def notify_info(message: str, context: dict[str, Any] | None = None) -> bool:
    return await notification_service.send("info", message, context)


async def notify_warning(message: str, context: dict[str, Any] | None = None) -> bool:
    return await notification_service.send("warning", message, context)


async def notify_error(message: str, context: dict[str, Any] | None = None) -> bool:
    return await notification_service.send("error", message, context)


async def notify_order_event(
    status: str,
    *,
    symbol: str,
    side: str,
    qty: float,
    price: float | None,
    order_id: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
) -> bool:
    level = "info" if status.upper() in {"FILLED", "SENT_PENDING"} else "warning"
    return await notification_service.send(
        level,
        f"Emir durumu: {status.upper()}",
        {
            "Sembol": symbol,
            "Yön": side,
            "Miktar": qty,
            "Fiyat": price,
            "Emir": order_id,
            "Neden": reason,
            "İstek": request_id,
        },
        event_key=f"order:{status.upper()}:{request_id or symbol}:{order_id or ''}",
    )


async def notify_gateway_event(
    event: str, context: dict[str, Any] | None = None
) -> bool:
    return await notification_service.send(
        "warning", f"Gateway: {event}", context, event_key=f"gateway:{event}"
    )


async def notify_risk_block(
    message: str, context: dict[str, Any] | None = None
) -> bool:
    return await notification_service.send(
        "warning", message, context, event_key=f"risk:{message}"
    )
