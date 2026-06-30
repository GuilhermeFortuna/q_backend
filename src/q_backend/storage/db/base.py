import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

# Decimal-safe persisted money/quantity/price (never binary float balances).
MoneyNumeric = Numeric(20, 8)
QuantityNumeric = Numeric(20, 8)
PriceNumeric = Numeric(20, 8)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PortableJSON(TypeDecorator):
    """JSONB on Postgres, JSON on SQLite (for unit tests)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
