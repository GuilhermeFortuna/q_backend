"""SQLAlchemy models for the market-data lake dataset catalog."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from q_backend.storage.db.base import Base, PortableJSON


class Dataset(Base):
    __tablename__ = "lake_datasets"

    dataset_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="", server_default="")
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    supersedes: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("lake_datasets.dataset_id"), nullable=True)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    published_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    checksum_algorithm: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="sha256", server_default="sha256"
    )
    arrow_schema: Mapped[dict] = mapped_column(PortableJSON, nullable=False)
    row_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    time_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    time_end: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    tombstoned_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    deletable_after: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    files: Mapped[list[DatasetFile]] = relationship(
        "DatasetFile",
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="DatasetFile.ordinal",
    )

    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('publishing', 'published', 'tombstoned', 'deleted')",
            name="ck_lake_datasets_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_lake_datasets_version"),
        sa.UniqueConstraint("kind", "symbol", "timeframe", "version", name="uq_lake_datasets_version"),
        sa.Index(
            "uq_lake_datasets_current_published",
            "kind",
            "symbol",
            "timeframe",
            unique=True,
            postgresql_where=sa.text("state = 'published'"),
            sqlite_where=sa.text("state = 'published'"),
        ),
    )


class DatasetFile(Base):
    __tablename__ = "lake_dataset_files"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("lake_datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(sa.String(1024), primary_key=True)
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    partition_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    partition_end: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    dataset: Mapped[Dataset] = relationship("Dataset", back_populates="files")

    __table_args__ = (sa.Index("ix_lake_dataset_files_dataset_ordinal", "dataset_id", "ordinal"),)
