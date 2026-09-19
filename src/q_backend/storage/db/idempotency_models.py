"""Persistence model for client-supplied command idempotency keys."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from q_backend.storage.db.base import Base, PortableJSON, utc_now


class CommandIdempotency(Base):
    """The committed result of one client command, retained for the replay TTL."""

    __tablename__ = "command_idempotency"

    key: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_command_idempotency_created_at", "created_at"),)
