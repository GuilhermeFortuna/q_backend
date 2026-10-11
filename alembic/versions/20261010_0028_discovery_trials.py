"""Persist structured Discovery trials.

Revision ID: 20261010_0028
Revises: 20261008_0027
"""

import sqlalchemy as sa
from alembic import op

revision = "20261010_0028"
down_revision = "20261008_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_search_trials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("strategy_search_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("objective_value", sa.Float()),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "run_id",
            "candidate_id",
            "generation",
            "window_index",
            "trial_number",
            name="uq_strategy_search_trial_identity",
        ),
    )
    op.create_index("ix_strategy_search_trials_run_state", "strategy_search_trials", ["run_id", "state"])


def downgrade() -> None:
    op.drop_table("strategy_search_trials")
