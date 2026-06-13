"""strategy search runs and candidates tables

Revision ID: 20260613_0004
Revises: 20260611_0003
Create Date: 2026-06-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0004"
down_revision: Union[str, Sequence[str], None] = "20260611_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_search_runs",
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
    op.create_index(
        "ix_strategy_search_runs_status",
        "strategy_search_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_search_runs_created_at",
        "strategy_search_runs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "strategy_search_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("objective_value", sa.Float(), nullable=True),
        sa.Column("robustness_score", sa.Float(), nullable=True),
        sa.Column("efficiency", sa.Float(), nullable=True),
        sa.Column("gate_flags", sa.JSON(), nullable=False),
        sa.Column("passed_gates", sa.Boolean(), nullable=False),
        sa.Column("oos_metrics", sa.JSON(), nullable=True),
        sa.Column("is_metrics_summary", sa.JSON(), nullable=True),
        sa.Column("best_params", sa.JSON(), nullable=True),
        sa.Column("window_count", sa.Integer(), nullable=False),
        sa.Column("completed_windows", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["strategy_search_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "candidate_id",
            name="uq_strategy_search_candidates_run_candidate",
        ),
    )


def downgrade() -> None:
    op.drop_table("strategy_search_candidates")
    op.drop_index("ix_strategy_search_runs_created_at", table_name="strategy_search_runs")
    op.drop_index("ix_strategy_search_runs_status", table_name="strategy_search_runs")
    op.drop_table("strategy_search_runs")
