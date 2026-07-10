"""Position sync — gateway'den çekilen pozisyonları ``bot_positions``'a yazar.

Eski mimaride bot pozisyonlarını sunucuya **push** ediyordu
(``POST /api/bot/positions/sync``). Full-inversion'da yön tersine döndü:
scanner her turda gateway'den pozisyonları **pull** edip bu tabloyu tazeler.

Tablo salt bir önbellek değil — admin panelinin Positions sayfası ve
"tümünü sat" acil durum akışı doğrudan buradan okuduğu için güncel kalması
operasyonel bir gerekliliktir.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.db import BotPosition
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)

logger = logging.getLogger(__name__)


async def sync_positions_from_gateway(gateway: MatriksGatewayClient) -> int:
    """Gateway'deki pozisyon anlık görüntüsünü ``bot_positions``'a upsert et.

    ``positionsLoaded=true`` yanıtı tam snapshot kabul edilir. Sıfır lotlu
    izleme sembolleri saklanmaz; snapshot'ta bulunmayan eski kayıtlar silinir.

    Returns:
        Yazılan/güncellenen satır sayısı. Gateway ulaşılamıyorsa veya isteği
        reddettiyse 0 (istisna fırlatmaz — tarama turunu bozmamalı).
    """
    try:
        snapshot = await gateway.get_positions()
    except (GatewayUnavailable, GatewayError) as exc:
        logger.warning("Position sync skipped: gateway error %s", exc)
        return 0

    if not snapshot.get("positionsLoaded"):
        logger.info("Position sync skipped: gateway has not loaded positions yet")
        return 0

    entries = snapshot.get("positions") or []
    positions: dict[str, float] = {}
    for entry in entries:
        symbol = str(entry.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        try:
            qty = float(entry.get("botQty", 0.0))
        except (TypeError, ValueError):
            logger.warning("Position sync ignored invalid qty symbol=%s", symbol)
            continue
        if qty > 0:
            positions[symbol] = qty

    synced = 0
    try:
        async with async_session_factory() as session:
            for symbol, qty in positions.items():
                row = (
                    await session.execute(
                        select(BotPosition).where(BotPosition.symbol == symbol)
                    )
                ).scalar_one_or_none()

                if row is None:
                    session.add(BotPosition(symbol=symbol, qty=qty))
                else:
                    row.qty = qty
                synced += 1
            if positions:
                await session.execute(
                    delete(BotPosition).where(BotPosition.symbol.not_in(positions))
                )
            else:
                await session.execute(delete(BotPosition))
            await session.commit()
    except Exception:
        logger.exception("Failed to persist positions from gateway")
        return 0

    logger.info("Positions synced from gateway count=%d", synced)
    return synced
