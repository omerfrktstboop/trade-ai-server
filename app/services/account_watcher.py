"""Hesap kimliği izleyicisi (v2 Faz 4).

Gateway /health yanıtındaki hash'li hesap kimliği alanlarını
(``accountRef``, ``accountSessionRef``, ``accountType``) ve kontrat sürümünü
izler. HERHANGİ bir değişiklikte:

- ``account_events`` tablosuna olay yazar (ACCOUNT_CHANGED / TYPE_CHANGED /
  SESSION_CHANGED / CONTRACT_MISMATCH),
- REAL hesap arming'ini otomatik düşürür (``disarm_real_account``),
- o tick için dispatch'i bloklar (fail-closed).

Karşılaştırmalar gateway'den gelen hash değerleriyle birebir yapılır —
Python tarafında yeniden hash yoktur. Watcher hiçbir koşulda exception
fırlatmaz; veri eksikliği "dispatch engellendi" sonucuna iner, sürece zarar
vermez.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AccountEvent
from app.services.admin_config import (
    disarm_real_account,
    get_admin_config_value,
    _parse_bool,
)

logger = logging.getLogger(__name__)

EXPECTED_CONTRACT_VERSION = 3


@dataclass(frozen=True)
class AccountCheckResult:
    dispatch_allowed: bool
    reason: str | None
    account_ref: str | None = None
    account_session_ref: str | None = None
    account_type: str | None = None


class AccountWatcher:
    """Son görülen hesap kimliğini süreç içinde tutar; değişim tespitinde
    olay + otomatik disarm üretir. Restart sonrası ilk gözlem baseline olur
    (değişim sayılmaz) — armedAccountRef karşılaştırması bundan bağımsız
    olarak her çağrıda yapılır."""

    def __init__(self) -> None:
        self._last_account_ref: str | None = None
        self._last_session_ref: str | None = None
        self._last_account_type: str | None = None

    def reset(self) -> None:
        self._last_account_ref = None
        self._last_session_ref = None
        self._last_account_type = None

    def current_account_ref(self) -> str | None:
        """Watcher'ın son gördüğü aktif hesap referansı (fill damgalama için)."""
        return self._last_account_ref

    async def check(
        self, health: dict[str, Any], session: AsyncSession
    ) -> AccountCheckResult:
        """Bir /health yanıtını değerlendir; dispatch izni + gerekçe döndür.

        Olay yazımı ve disarm bu çağrının içinde yapılır (caller commit'i
        üstlenir — mevcut session'a eklenir).
        """
        try:
            return await self._check_inner(health, session)
        except Exception as exc:  # noqa: BLE001 — watcher asla süreci düşürmez
            logger.exception("Account watcher check failed")
            return AccountCheckResult(False, f"account watcher error: {exc}")

    async def _check_inner(
        self, health: dict[str, Any], session: AsyncSession
    ) -> AccountCheckResult:
        contract = health.get("gatewayContractVersion")
        if contract != EXPECTED_CONTRACT_VERSION:
            await self._record(
                session,
                "CONTRACT_MISMATCH",
                detail=f"gatewayContractVersion={contract!r} expected={EXPECTED_CONTRACT_VERSION}",
            )
            return AccountCheckResult(
                False, f"gateway contract version mismatch: {contract!r}"
            )

        account_ref = _clean(health.get("accountRef"))
        session_ref = _clean(health.get("accountSessionRef"))
        account_type = _clean(health.get("accountType")) or "UNKNOWN"

        if not account_ref or not session_ref or account_type == "UNKNOWN":
            # Kimlik doğrulanamıyor → emir yok; olay üretmeye gerek yok
            # (gateway zaten fail-closed).
            return AccountCheckResult(
                False, "account identity unavailable from gateway health"
            )

        changed_events: list[tuple[str, str | None]] = []
        if self._last_account_ref is not None:
            if account_ref != self._last_account_ref:
                changed_events.append(("ACCOUNT_CHANGED", self._last_account_ref))
            if (
                self._last_account_type is not None
                and account_type != self._last_account_type
            ):
                changed_events.append(("TYPE_CHANGED", self._last_account_ref))
            if (
                self._last_session_ref is not None
                and session_ref
                and session_ref != self._last_session_ref
            ):
                changed_events.append(("SESSION_CHANGED", self._last_account_ref))

        armed = _parse_bool(await get_admin_config_value(session, "realAccountArmed"))
        armed_ref = (
            await get_admin_config_value(session, "armedAccountRef")
        ).strip()
        armed_session_ref = (
            await get_admin_config_value(session, "armedAccountSessionRef")
        ).strip()

        if changed_events:
            for event_type, previous_ref in changed_events:
                await self._record(
                    session,
                    event_type,
                    account_ref=account_ref,
                    account_session_ref=session_ref,
                    account_type=account_type,
                    previous_ref=previous_ref,
                    detail="detected by account watcher on gateway health",
                )
            if armed:
                await disarm_real_account(
                    session,
                    f"auto-disarm: {', '.join(e for e, _ in changed_events)}",
                )
                await self._record(
                    session,
                    "DISARMED",
                    account_ref=account_ref,
                    account_session_ref=session_ref,
                    account_type=account_type,
                    previous_ref=armed_ref or None,
                    detail="auto-disarm after account identity change",
                )
            self._remember(account_ref, session_ref, account_type)
            return AccountCheckResult(
                False,
                "account identity changed: " + ", ".join(e for e, _ in changed_events),
                account_ref=account_ref,
                account_session_ref=session_ref,
                account_type=account_type,
            )

        self._remember(account_ref, session_ref, account_type)

        # Arm edilmiş REAL hesap referansı VEYA oturum referansı canlı hesapla
        # uyuşmuyorsa disarm (restart sonrası baseline yokken bile — in-memory
        # baseline'a bağlı değil, DB'deki arm anındaki değerlerle karşılaştırır).
        mismatch_reason = None
        if armed and armed_ref and account_ref != armed_ref:
            mismatch_reason = "live accountRef does not match armedAccountRef"
        elif (
            armed
            and armed_session_ref
            and session_ref
            and session_ref != armed_session_ref
        ):
            mismatch_reason = "live accountSessionRef does not match armed session"
        if mismatch_reason is not None:
            await disarm_real_account(
                session, f"auto-disarm: {mismatch_reason}"
            )
            await self._record(
                session,
                "DISARMED",
                account_ref=account_ref,
                account_session_ref=session_ref,
                account_type=account_type,
                previous_ref=armed_ref,
                detail=mismatch_reason,
            )
            return AccountCheckResult(
                False,
                "armed account/session mismatch — auto-disarmed",
                account_ref=account_ref,
                account_session_ref=session_ref,
                account_type=account_type,
            )

        return AccountCheckResult(
            True,
            None,
            account_ref=account_ref,
            account_session_ref=session_ref,
            account_type=account_type,
        )

    def _remember(
        self, account_ref: str, session_ref: str | None, account_type: str
    ) -> None:
        self._last_account_ref = account_ref
        self._last_session_ref = session_ref
        self._last_account_type = account_type

    async def _record(
        self,
        session: AsyncSession,
        event_type: str,
        *,
        account_ref: str | None = None,
        account_session_ref: str | None = None,
        account_type: str | None = None,
        previous_ref: str | None = None,
        detail: str | None = None,
    ) -> None:
        session.add(
            AccountEvent(
                event_type=event_type,
                account_ref=account_ref,
                account_session_ref=account_session_ref,
                account_type=account_type,
                previous_ref=previous_ref,
                source="WATCHER",
                detail=detail,
            )
        )
        logger.warning(
            "ACCOUNT_EVENT type=%s accountRef=%s accountType=%s detail=%s",
            event_type,
            account_ref,
            account_type,
            detail,
        )


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


#: Modül seviyesinde paylaşılan izleyici — scanner ve emir yolu bunu kullanır.
account_watcher = AccountWatcher()
