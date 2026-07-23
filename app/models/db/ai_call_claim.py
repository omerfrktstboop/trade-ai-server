"""AI call claim — kalıcı, bar-farkında LLM çağrı kilidi (Plan Faz 1.2).

Aynı Min5 barı içinde setup anlamlı biçimde değişmedikçe aynı sembol için
tekrar LLM'e sorulmamalıdır. Bu kaydı DB'de tutmak, decision_gate'in süreç-içi
cache'inin aksine restart'ı da hayatta bırakır: yeniden başlayan sunucu aynı
bar/setup için token harcayan çağrıyı tekrarlamaz.

Bir satır = "(sembol, bar, setup parmak izi) için LLM çağrısı bir kez talep
edildi". Benzersizlik kısıtı atomik talep içindir: satır ilk kez eklenebilirse
çağrı yapılır; ekleme çakışırsa çağrı zaten yapılmıştır ve atlanır.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AiCallClaim(Base):
    __tablename__ = "ai_call_claims"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "bar_key",
            "setup_fingerprint",
            name="uq_ai_call_claim_symbol_bar_setup",
        ),
        Index("ix_ai_call_claims_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Min5 barının kimliği (periyot sınırına yuvarlanmış epoch ya da bar
    # etiketi). Aynı bar içindeki tüm gözlemler aynı bar_key'i paylaşır.
    bar_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Setup'ın maddi girdilerinin kısa hash'i. Setup anlamlı biçimde
    # değişirse parmak izi değişir ve aynı bar içinde yeni bir çağrıya izin
    # verilir.
    setup_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_purpose: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
