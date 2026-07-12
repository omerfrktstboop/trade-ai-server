"""Gateway smoke test — çalışan TradeAiGateway'e karşı uçtan uca doğrulama.

Matriks IQ içinde TradeAiGateway algo'su çalışırken bu script'i aynı
makinede koştur::

    python scripts/gateway_smoke.py
    python scripts/gateway_smoke.py --url http://127.0.0.1:8787 --token GATEWAY_TOKEN
    python scripts/gateway_smoke.py --symbol THYAO --symbol AKBNK

URL/token verilmezse .env'deki MATRIKS_GATEWAY_URL / MATRIKS_GATEWAY_TOKEN
kullanılır. Script hiçbir emir göndermez — gateway zaten read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows konsolu varsayılan cp1254 ile ✓/─ karakterlerini basamıyor.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.services.matriks_gateway import (  # noqa: E402
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}\n{title}\n{'─' * 60}")


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


async def run(url: str | None, token: str | None, symbols: list[str]) -> int:
    client = MatriksGatewayClient(base_url=url, token=token)
    failures = 0

    try:
        # 1. Health
        _print_section("GET /health")
        try:
            health = await client.health()
            _print_json(health)
            if not health.get("subscriptionsInitialized"):
                print("⚠ subscriptionsInitialized=false — semboller henüz abone değil")
            if not health.get("positionsLoaded"):
                print("⚠ positionsLoaded=false — pozisyon snapshot'ı henüz gelmedi")
        except GatewayUnavailable as exc:
            print(f"✗ Gateway'e ulaşılamıyor: {exc}")
            print("  Matriks IQ açık mı? TradeAiGateway algo'su çalışıyor mu?")
            return 1
        except GatewayError as exc:
            print(f"✗ Health hatası: {exc}")
            print("  Token doğru mu? (--token veya MATRIKS_GATEWAY_TOKEN)")
            return 1
        print("✓ health OK")

        # Sembol listesi verilmediyse gateway'in kendi listesini kullan
        if not symbols:
            symbols = list(health.get("symbols") or [])
            if not symbols:
                print("✗ Gateway sembol listesi boş; --symbol ile belirt")
                return 1

        # 2. Snapshot — her sembol için
        for symbol in symbols:
            _print_section(f"GET /snapshot?symbol={symbol}")
            try:
                snapshot = await client.get_snapshot(symbol)
            except (GatewayUnavailable, GatewayError) as exc:
                print(f"✗ Snapshot hatası: {exc}")
                failures += 1
                continue

            payload = snapshot.get("payload") or {}
            _print_json(snapshot)

            checks = {
                "lastPrice > 0": (payload.get("lastPrice") or 0) > 0,
                "quoteReliable": bool(payload.get("quoteReliable")),
                "ohlcReliable": bool(payload.get("ohlcReliable")),
                "depthReliable": bool(payload.get("depthReliable")),
                "rsi mevcut": payload.get("rsi") is not None,
                "technicalFeatures mevcut": isinstance(
                    payload.get("technicalFeatures"), dict
                ),
            }
            for name, passed in checks.items():
                # Veri güvenilirlik bayrakları piyasa kapalıyken false olabilir;
                # bunlar hata değil uyarıdır. lastPrice=0 ise gerçek sorun var.
                mark = "✓" if passed else "⚠"
                print(f"  {mark} {name}")
            if (payload.get("lastPrice") or 0) <= 0:
                print(
                    "  ✗ lastPrice=0 — veri akışı yok (piyasa kapalı veya abonelik sorunu)"
                )
                failures += 1

        # 3. Positions
        _print_section("GET /positions")
        try:
            positions = await client.get_positions()
            _print_json(positions)
            print("✓ positions OK")
        except (GatewayUnavailable, GatewayError) as exc:
            print(f"✗ Positions hatası: {exc}")
            failures += 1

    finally:
        await client.close()

    _print_section("SONUÇ")
    if failures:
        print(f"✗ {failures} kontrol başarısız")
        return 1
    print("✓ Tüm kontroller geçti")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TradeAiGateway smoke test")
    parser.add_argument("--url", default=None, help="Gateway URL (default: .env)")
    parser.add_argument("--token", default=None, help="Gateway token (default: .env)")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Test edilecek sembol (tekrarlanabilir; default: gateway'in listesi)",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run(args.url, args.token, [s.upper() for s in args.symbol]))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
