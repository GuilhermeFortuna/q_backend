"""walkforward runs and windows tables

Revision ID: 20260611_0003
Revises: 20260609_0002
Create Date: 2026-06-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260611_0003"
down_revision: Union[str, Sequence[str], None] = "20260609_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "walkforward_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("lake_paths", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_walkforward_runs_status", "walkforward_runs", ["status"], unique=False)
    op.create_index(
        "ix_walkforward_runs_created_at",
        "walkforward_runs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "walkforward_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("window_number", sa.Integer(), nullable=False),
        sa.Column("train_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("best_params", sa.JSON(), nullable=False),
        sa.Column("is_metrics", sa.JSON(), nullable=True),
        sa.Column("oos_metrics", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["walkforward_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "window_number", name="uq_walkforward_windows_run_window"),
    )


def downgrade() -> None:
    op.drop_table("walkforward_windows")
    op.drop_index("ix_walkforward_runs_created_at", table_name="walkforward_runs")
    op.drop_index("ix_walkforward_runs_status", table_name="walkforward_runs")
    op.drop_table("walkforward_runs")
