"""Matriks gateway client — full-inversion mimarisinin server tarafı.

Aynı makinede Matriks IQ içinde çalışan TradeAiGateway algo'suna
(``matriks/TradeAiGateway.cs``) HTTP üzerinden konuşur. Gateway sadece
loopback'te dinlediği için URL her zaman ``http://127.0.0.1:<port>`` olur.

Phase 0 kapsamı: read-only çağrılar (health / snapshot / positions).
Emir gönderme Phase 2'de eklenecek.

Kullanım::

    client = MatriksGatewayClient()
    health = await client.health()
    snapshot = await client.get_snapshot("THYAO")
    positions = await client.get_positions()
    await client.close()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class GatewayUnavailable(Exception):
    """Gateway'e ulaşılamıyor (Matriks kapalı, algo durmuş veya timeout).

    Scanner bu hatayı yakalayıp o tarama turunu atlar — asla yukarı
    fırlatılıp süreci düşürmemeli.
    """


class GatewayError(Exception):
    """Gateway ulaşılabilir ama isteği reddetti (4xx/5xx veya ok=false)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Gateway HTTP {status_code}: {message}")


class MatriksGatewayClient:
    """TradeAiGateway'e konuşan async HTTP client.

    Her çağrı ağ hatasında bir kez yeniden denenir; ikinci hata
    ``GatewayUnavailable`` olarak fırlatılır.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.matriks_gateway_url).rstrip("/")
        self._token = token if token is not None else settings.matriks_gateway_token
        self._timeout = timeout if timeout is not None else settings.matriks_gateway_timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """GET /health — Matriks/veri/pozisyon durumu."""
        return await self._get("/health")

    async def get_snapshot(self, symbol: str) -> dict[str, Any]:
        """GET /snapshot — sembol için OHLCV + derinlik + teknik feature bloğu.

        Dönen dict'in ``payload`` alanı, eski bot'un ``BuildMarketData``
        çıktısıyla aynı şemadadır (lastPrice, open/high/low, rsi, ema20/50,
        macd, technicalFeatures, botPositionQty, ...).
        """
        return await self._get("/snapshot", params={"symbol": symbol.strip().upper()})

    async def get_positions(self) -> dict[str, Any]:
        """GET /positions — pozisyon anlık görüntüsü.

        Dönen dict: ``positionsLoaded`` bayrağı + sembol başına
        ``botQty`` / ``lockedLongTermQty`` / ``totalQty`` listesi.
        """
        return await self._get("/positions")

    async def get_capabilities(self) -> dict[str, Any]:
        """Return the data surfaces supported by the running gateway."""
        return await self._get("/capabilities")

    async def get_depth(self, symbol: str, levels: int = 25) -> dict[str, Any]:
        """Return up to 25 bid/ask levels plus aggregate imbalance metrics."""
        return await self._get(
            "/depth",
            params={
                "symbol": symbol.strip().upper(),
                "levels": str(max(1, min(25, levels))),
            },
        )

    async def get_indicators(self, symbol: str) -> dict[str, Any]:
        """Return native/fallback technical indicators for one symbol."""
        return await self._get(
            "/indicators", params={"symbol": symbol.strip().upper()}
        )

    async def get_news(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return live Matriks news cached since gateway startup."""
        params = {"limit": str(max(1, min(200, limit)))}
        if symbol:
            params["symbol"] = symbol.strip().upper()
        return await self._get("/news", params=params)

    async def get_institutions(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        """Return daily ranked AKD buyers/sellers when licensed."""
        return await self._get(
            "/institutions",
            params={
                "symbol": symbol.strip().upper(),
                "limit": str(max(1, min(20, limit))),
            },
        )

    async def get_mkk(self) -> dict[str, Any]:
        """Return explicit MKK/Takas capability status from the gateway."""
        return await self._get("/mkk")

    async def send_order(
        self,
        *,
        request_id: str,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        mode: str,
    ) -> dict[str, Any]:
        """POST /order — gateway'e LIMIT emir isteği gönder.

        Gateway kendi güvenlik kilitlerini uygular; sonuç senkron döner:
        ``{"accepted": true, "status": "SENT_PENDING"}`` veya
        ``{"accepted": false, "status": "REJECTED", "reason": ...}``.
        Nihai borsa durumu (FILLED vb.) gateway'in ``OnOrderUpdate`` →
        ``/api/order-result`` raporuyla ayrıca gelir.

        Retry YAPILMAZ: gönderim sonrası kopan bağlantıda tekrar denemek ya
        çift emir basar ya da gateway'in duplicate-requestId koruması yüzünden
        gerçekte gönderilmiş emri REJECTED sanmamıza yol açar. Tek deneme;
        ağ hatası → ``GatewayUnavailable``.
        """
        client = self._ensure_client()
        body = {
            "requestId": request_id,
            "symbol": symbol.strip().upper(),
            "side": side.strip().upper(),
            "qty": qty,
            "limitPrice": limit_price,
            "mode": mode.strip().upper(),
        }
        try:
            response = await client.post("/order", json=body)
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            raise GatewayUnavailable(
                f"Gateway unreachable at {self._base_url}/order: {exc}"
            ) from exc

        if response.status_code != 200:
            raise GatewayError(response.status_code, response.text)

        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error", "unknown") if isinstance(data, dict) else "invalid response"
            raise GatewayError(response.status_code, str(error))

        return data

    async def reload_config(self) -> dict[str, Any]:
        """Ask the running gateway to immediately re-fetch server config."""
        client = self._ensure_client()
        try:
            response = await client.post("/config/reload")
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            raise GatewayUnavailable(f"Gateway config reload failed: {exc}") from exc
        if response.status_code != 200:
            raise GatewayError(response.status_code, response.text)
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise GatewayError(response.status_code, str(data))
        return data

    async def is_available(self) -> bool:
        """Gateway ayakta ve sağlıklı mı? Exception fırlatmaz."""
        try:
            health = await self.health()
        except (GatewayUnavailable, GatewayError):
            return False
        return bool(health.get("ok"))

    async def close(self) -> None:
        """Alttaki HTTP bağlantı havuzunu kapat."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Internals ──────────────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
                transport=self._transport,
            )
        return self._client

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        client = self._ensure_client()

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await client.get(path, params=params)
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning(
                    "Gateway request failed path=%s attempt=%d error=%s",
                    path,
                    attempt,
                    exc,
                )
                continue

            if response.status_code != 200:
                raise GatewayError(response.status_code, response.text)

            data = response.json()
            if not isinstance(data, dict) or not data.get("ok"):
                error = data.get("error", "unknown") if isinstance(data, dict) else "invalid response"
                raise GatewayError(response.status_code, str(error))

            return data

        raise GatewayUnavailable(
            f"Gateway unreachable at {self._base_url}{path}: {last_error}"
        ) from last_error


# Modül seviyesinde paylaşılan tek client — scanner ve router'lar bunu kullanır.
gateway_client = MatriksGatewayClient()
