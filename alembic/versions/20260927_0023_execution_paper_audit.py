"""Persist dispatch attempt audit timestamps.

Revision ID: 20260927_0023
Revises: 20260927_0022
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from q_backend.storage.settings import get_settings

revision: str = "20260927_0023"
down_revision: Union[str, Sequence[str], None] = "20260927_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_orders",
        sa.Column("dispatch_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    settings = get_settings()
    defaults = {
        "point_value": str(settings.execution_default_point_value),
        "slippage_points": str(settings.execution_paper_slippage_points),
        "cost_per_contract": str(settings.execution_paper_cost_per_contract),
        "cost_bps": str(settings.execution_paper_cost_bps),
    }
    deployments = sa.table(
        "execution_deployments",
        sa.column("id", sa.Uuid()),
        sa.column("paper_cost_config", sa.JSON()),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(deployments.c.id, deployments.c.paper_cost_config)):
        if not row.paper_cost_config or not row.paper_cost_config.get("point_value"):
            connection.execute(
                deployments.update().where(deployments.c.id == row.id).values(paper_cost_config=defaults)
            )
    op.create_table(
        "execution_paper_marks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("bar_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("mark_status", sa.String(length=32), nullable=False),
        sa.Column("quote_bid", sa.Numeric(20, 8), nullable=True),
        sa.Column("quote_ask", sa.Numeric(20, 8), nullable=True),
        sa.Column("quote_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_source", sa.String(length=64), nullable=True),
        sa.Column("mark_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 8), nullable=False),
        sa.Column("fees", sa.Numeric(20, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["deployment_id"], ["execution_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "bar_close_time", name="uq_execution_paper_marks_deployment_bar"),
    )
    op.create_index(
        "ix_execution_paper_marks_deployment_time",
        "execution_paper_marks",
        ["deployment_id", "bar_close_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_paper_marks_deployment_time", table_name="execution_paper_marks")
    op.drop_table("execution_paper_marks")
    op.drop_column("execution_orders", "dispatch_attempted_at")
