"""Tests for best-effort Telegram notifications."""

from __future__ import annotations

import pytest
import httpx

from app.services.notifications import NotificationService


@pytest.mark.asyncio
async def test_disabled_notification_does_not_call_telegram() -> None:
    service = NotificationService(token="", chat_id="")

    assert await service.send("info", "test") is False


@pytest.mark.asyncio
async def test_notification_posts_short_message() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    service = NotificationService(
        token="token", chat_id="chat", transport=httpx.MockTransport(handler)
    )
    sent = await service.send("warning", "Gateway kapalı", {"Sembol": "THYAO"})

    assert sent is True
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/bottoken/sendMessage")


@pytest.mark.asyncio
async def test_notification_throttles_same_event() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    service = NotificationService(
        token="token", chat_id="chat", transport=httpx.MockTransport(handler)
    )
    assert await service.send("warning", "Gateway kapalı", event_key="gateway:offline")
    assert not await service.send(
        "warning", "Gateway kapalı", event_key="gateway:offline"
    )
    assert calls == 1
