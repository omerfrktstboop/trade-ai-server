"""Singleton row used to serialize cash reservations across DB sessions."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountReservationScope(Base):
    __tablename__ = "account_reservation_scopes"

    scope_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
