"""evaluation run and feature score tables

Revision ID: 20260626_0010
Revises: 20260626_0009
Create Date: 2026-06-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260626_0010"
down_revision: Union[str, Sequence[str], None] = "20260626_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_name", sa.String(length=64), nullable=False),
        sa.Column("target_horizon", sa.Integer(), nullable=False),
        sa.Column("matrix_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"], unique=False)
    op.create_index("ix_evaluation_runs_created_at", "evaluation_runs", ["created_at"], unique=False)

    op.create_table(
        "feature_score_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("feature_id", sa.String(length=128), nullable=False),
        sa.Column("feature_name", sa.String(length=255), nullable=False),
        sa.Column("ic", sa.Float(), nullable=True),
        sa.Column("rank_ic", sa.Float(), nullable=True),
        sa.Column("mutual_info", sa.Float(), nullable=True),
        sa.Column("stability", sa.Float(), nullable=True),
        sa.Column("global_score", sa.Float(), nullable=True),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("is_representative", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("leakage_status", sa.String(length=32), nullable=False),
        sa.Column("regime_ics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "feature_id", name="uq_feature_score_rows_run_feature"),
    )
    op.create_index(
        "ix_feature_score_rows_feature_name",
        "feature_score_rows",
        ["feature_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_feature_score_rows_feature_name", table_name="feature_score_rows")
    op.drop_table("feature_score_rows")
    op.drop_index("ix_evaluation_runs_created_at", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
