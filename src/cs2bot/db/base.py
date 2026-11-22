"""Declarative base, portable column types and mixins.

Types are chosen to run on PostgreSQL (production) and SQLite (unit tests):
* JSON  -> JSONB on PostgreSQL, JSON elsewhere
* enums -> VARCHAR + CHECK-free validation in Python (``native_enum=False``) so new values
  don't need a migration-time ALTER TYPE
* timestamps are always timezone-aware UTC
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, Numeric, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(14, 2)
OddsNum = Numeric(8, 3)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that always comes back as UTC (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime column - use cs2bot.utils.time.utcnow()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def str_enum(enum_cls: type[StrEnum], length: int = 32) -> SAEnum:
    """Portable enum column storing the member *values*."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
        datetime: UTCDateTime(),
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
