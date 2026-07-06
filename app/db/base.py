"""SQLAlchemy declarative base — all ORM models inherit from this."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared base for all ORM models."""
