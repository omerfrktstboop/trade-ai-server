"""Süreç seviyesinde sert güvenlik bayrakları (v2).

Bu bayraklar DB'den bağımsızdır: DB yazımı/okuması başarısız olsa bile emir
gönderimini fail-closed durdurabilmek için süreç belleğinde tutulur.

Kullanım: startup'ta REAL hesap disarm işlemi başarısız olursa dispatch sert
bloklanır (``block_dispatch``). Scanner emir yolu her denemede
``is_dispatch_blocked`` kontrol eder; bloklu ise hiçbir emir gönderilmez —
analiz/gözlem (OBSERVE_ONLY davranışı) devam eder.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_dispatch_blocked: bool = False
_dispatch_block_reason: str | None = None


def block_dispatch(reason: str) -> None:
    """Emir gönderimini süreç ömrü boyunca sert biçimde durdur (fail-closed)."""
    global _dispatch_blocked, _dispatch_block_reason
    _dispatch_blocked = True
    _dispatch_block_reason = reason
    logger.error("DISPATCH_HARD_BLOCKED reason=%s", reason)


def clear_dispatch_block() -> None:
    """Sert bloğu kaldır — yalnızca testler ve bilinçli operatör aksiyonu."""
    global _dispatch_blocked, _dispatch_block_reason
    _dispatch_blocked = False
    _dispatch_block_reason = None


def is_dispatch_blocked() -> bool:
    return _dispatch_blocked


def dispatch_block_reason() -> str | None:
    return _dispatch_block_reason
