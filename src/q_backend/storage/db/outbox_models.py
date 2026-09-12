from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from q_backend.storage.db.base import Base, PortableJSON, utc_now


class OutboxEvent(Base):
    __tablename__ = "stream_outbox"

    topic: Mapped[str] = mapped_column(String(64), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    producer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    origin_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    routing_key: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    payload_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_schema: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class OutboxTopicState(Base):
    __tablename__ = "stream_outbox_topic_state"

    topic: Mapped[str] = mapped_column(String(64), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    last_relayed_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=sa.text("0"))
