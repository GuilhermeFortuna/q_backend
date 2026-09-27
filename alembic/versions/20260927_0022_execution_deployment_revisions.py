"""execution deployment configuration revisions and catalog provenance

Revision ID: 20260927_0022
Revises: 20260918_0021
Create Date: 2026-09-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0022"
down_revision: Union[str, Sequence[str], None] = "20260918_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_deployments",
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "execution_deployments",
        sa.Column("paper_cost_config", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "execution_deployments",
        sa.Column("activation_cutoff_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_deployments",
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="builtin"),
    )
    op.add_column(
        "execution_deployments",
        sa.Column("source_strategy_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "execution_decisions",
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "execution_decisions",
        sa.Column("paper_cost_config", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "execution_deployment_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("strategy_name", sa.String(length=255), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("compiled_config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("sizing_config", sa.JSON(), nullable=False),
        sa.Column("risk_config", sa.JSON(), nullable=False),
        sa.Column("paper_cost_config", sa.JSON(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_strategy_name", sa.String(length=255), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["execution_deployments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "revision",
            name="uq_execution_deployment_revisions_deployment_revision",
        ),
    )
    op.create_index(
        "ix_execution_deployment_revisions_deployment",
        "execution_deployment_revisions",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_deployment_revisions_deployment",
        table_name="execution_deployment_revisions",
    )
    op.drop_table("execution_deployment_revisions")
    op.drop_column("execution_decisions", "paper_cost_config")
    op.drop_column("execution_decisions", "config_revision")
    op.drop_column("execution_deployments", "source_strategy_name")
    op.drop_column("execution_deployments", "source_kind")
    op.drop_column("execution_deployments", "activation_cutoff_at")
    op.drop_column("execution_deployments", "paper_cost_config")
    op.drop_column("execution_deployments", "config_revision")
