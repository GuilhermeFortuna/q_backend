"""lake dataset catalog tables

Revision ID: 20260915_0019
Revises: 20260913_0018
Create Date: 2026-09-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260915_0019"
down_revision: Union[str, Sequence[str], None] = "20260913_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lake_datasets",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checksum_algorithm", sa.String(length=32), nullable=False, server_default="sha256"),
        sa.Column("arrow_schema", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("time_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletable_after", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("dataset_id", name="pk_lake_datasets"),
        sa.ForeignKeyConstraint(["supersedes"], ["lake_datasets.dataset_id"], name="fk_lake_datasets_supersedes"),
        sa.CheckConstraint(
            "state IN ('publishing', 'published', 'tombstoned', 'deleted')",
            name="ck_lake_datasets_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_lake_datasets_version"),
        sa.UniqueConstraint("kind", "symbol", "timeframe", "version", name="uq_lake_datasets_version"),
    )
    op.create_index(
        "uq_lake_datasets_current_published",
        "lake_datasets",
        ["kind", "symbol", "timeframe"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
        sqlite_where=sa.text("state = 'published'"),
    )

    op.create_table(
        "lake_dataset_files",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column("partition_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("partition_end", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", "path", name="pk_lake_dataset_files"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["lake_datasets.dataset_id"],
            name="fk_lake_dataset_files_dataset_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_lake_dataset_files_dataset_ordinal",
        "lake_dataset_files",
        ["dataset_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index("ix_lake_dataset_files_dataset_ordinal", table_name="lake_dataset_files")
    op.drop_table("lake_dataset_files")
    op.drop_index("uq_lake_datasets_current_published", table_name="lake_datasets")
    op.drop_table("lake_datasets")
