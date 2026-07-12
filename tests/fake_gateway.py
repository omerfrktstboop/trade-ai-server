"""Fake TradeAiGateway — testler için httpx.MockTransport tabanlı stub.

Gerçek gateway'in (matriks/TradeAiGateway.cs) HTTP davranışını taklit eder:
bearer token kontrolü, /health, /snapshot?symbol=, /positions ve read-only
olduğu için /order'a 404. Şemalar gateway'in döndürdüğüyle birebir aynı
tutulmalıdır — evaluator/scanner testleri bu stub'a karşı koşacak.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_TOKEN = "test-gateway-token"
DEFAULT_SYMBOLS = ["THYAO", "AKBNK"]


def make_snapshot_payload(symbol: str, **overrides: Any) -> dict[str, Any]:
    """Gerçek gateway'in BuildMarketData çıktısıyla aynı şemada payload üret."""
    technical_features = {
        "schemaVersion": "technical-features-v1",
        "indicatorSource": "MATRIX_NATIVE_OR_READY",
        "alphaTrendSignal": "NEUTRAL",
        "alphaTrendMode": "PROXY_EMA_MACD_RSI",
        "indicatorBuyCount": 2,
        "indicatorSellCount": 1,
        "indicatorNeutralCount": 2,
        "indicatorConsensus": "NEUTRAL",
        "indicatorConsensusRatio": 0.4,
        "atr": 0.55,
        "natr": 0.77,
        "depthReliable": True,
        "depthBid1Size": 1500.0,
        "depthBid1MaxSize": 2000.0,
        "depthQueueDropPct": 25.0,
        "marketRegime": "NEUTRAL",
    }
    payload: dict[str, Any] = {
        "lastPrice": 71.5,
        "open": 71.0,
        "high": 72.0,
        "low": 70.8,
        "ohlcReliable": True,
        "ohlcSource": "BAR",
        "priceSource": "LIVE",
        "quoteReliable": True,
        "depthReliable": True,
        "volume": 12000.0,
        "rsi": 55.0,
        "ema20": 71.2,
        "ema50": 70.9,
        "macd": 0.12,
        "macdSignal": 0.08,
        "indicatorSource": "MATRIX_NATIVE_OR_READY",
        "bidPrice": 71.45,
        "askPrice": 71.55,
        "bidVolume": 1500.0,
        "askVolume": 900.0,
        "bestBid": 71.45,
        "secondBid": 71.4,
        "thirdBid": 71.35,
        "depthSummary": "bestBid=71.45;depthReliable=True",
        "botPositionQty": 0.0,
        "totalAccountQty": 100.0,
        "lockedLongTermQty": 100.0,
        "technicalFeatures": technical_features,
    }
    payload.update(technical_features)
    payload.update(overrides)
    return payload


class FakeGateway:
    """Programlanabilir fake gateway.

    Kullanım::

        fake = FakeGateway()
        client = MatriksGatewayClient(
            base_url="http://fake-gateway", token=fake.token,
            transport=fake.transport,
        )
    """

    def __init__(
        self,
        token: str = DEFAULT_TOKEN,
        symbols: list[str] | None = None,
    ) -> None:
        self.token = token
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.positions_loaded = True
        self.request_log: list[httpx.Request] = []
        # sembol → snapshot payload override'ları
        self.snapshot_overrides: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = [
            {"symbol": "THYAO", "botQty": 0.0, "lockedLongTermQty": 100.0, "totalQty": 100.0},
            {"symbol": "AKBNK", "botQty": 25.0, "lockedLongTermQty": 0.0, "totalQty": 25.0},
        ]
        # /order davranışı: None → kabul (SENT_PENDING); string → red gerekçesi
        self.order_rejection: str | None = None
        # Gateway'e ulaşan emir istekleri (parse edilmiş body'ler)
        self.orders: list[dict[str, Any]] = []
        self.order_states: list[dict[str, Any]] = []
        self.cancelled_order_ids: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    # ── Request handling ───────────────────────────────────────────────────

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.request_log.append(request)
        path = request.url.path

        if path == "/ping":
            return self._json(200, {"ok": True, "message": "pong", "server": "TradeAiGateway"})

        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return self._json(401, {"ok": False, "error": "unauthorized"})

        if path == "/health":
            return self._json(
                200,
                {
                    "ok": True,
                    "server": "TradeAiGateway",
                    "phase": "read-only",
                    "requestCount": len(self.request_log),
                    "symbols": self.symbols,
                    "subscriptionsInitialized": True,
                    "positionsLoaded": self.positions_loaded,
                    "autoOrderEnabled": True,
                    "testAutoOrderEnabled": True,
                    "quoteAgeSeconds": {s: 5.0 for s in self.symbols},
                },
            )

        if path == "/snapshot":
            symbol = (request.url.params.get("symbol") or "").upper()
            if not symbol:
                return self._json(400, {"ok": False, "error": "missing query parameter: symbol"})
            if symbol not in self.symbols:
                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": "symbol not in allowed list",
                        "symbol": symbol,
                        "allowedSymbols": self.symbols,
                    },
                )
            overrides = self.snapshot_overrides.get(symbol, {})
            return self._json(
                200,
                {
                    "ok": True,
                    "symbol": symbol,
                    "dataType": "OHLCV",
                    "timestamp": "2026-07-09T10:00:00+03:00",
                    "payload": make_snapshot_payload(symbol, **overrides),
                },
            )

        if path == "/positions":
            return self._json(
                200,
                {
                    "ok": True,
                    "positionsLoaded": self.positions_loaded,
                    "confidence": "HIGH" if self.positions_loaded else "LOW",
                    "snapshotCompleteFlag": self.positions_loaded,
                    "snapshotNonEmpty": bool(self.positions),
                    "snapshotGeneration": 1,
                    "positions": self.positions,
                },
            )

        if path == "/order" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            self.orders.append(body)
            if self.order_rejection is not None:
                return self._json(
                    200,
                    {
                        "ok": True,
                        "accepted": False,
                        "status": "REJECTED",
                        "requestId": body.get("requestId"),
                        "symbol": body.get("symbol"),
                        "reason": self.order_rejection,
                    },
                )
            return self._json(
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "status": "SENT_PENDING",
                    "requestId": body.get("requestId"),
                    "symbol": body.get("symbol"),
                    "reason": "Limit order SENT_PENDING; final status will be reported by OnOrderUpdate",
                },
            )

        if path == "/orders/active" and request.method == "GET":
            return self._json(200, {"ok": True, "available": True, "orders": self.order_states})

        if path == "/order/cancel" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            order_id = str(body.get("orderId") or "")
            self.cancelled_order_ids.append(order_id)
            return self._json(200, {"ok": True, "accepted": True, "status": "CANCEL_REQUESTED", "orderId": order_id})

        return self._json(404, {"ok": False, "error": "not found", "path": path})

    @staticmethod
    def _json(status_code: int, payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=json.dumps(payload),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
