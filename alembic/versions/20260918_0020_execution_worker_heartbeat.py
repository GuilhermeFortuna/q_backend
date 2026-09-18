"""execution worker heartbeat table

Revision ID: 20260918_0020
Revises: 20260915_0019
Create Date: 2026-09-18

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260918_0020"
down_revision: Union[str, Sequence[str], None] = "20260915_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("edge_reachable", sa.Boolean(), nullable=False),
        sa.Column("edge_mt5_connected", sa.Boolean(), nullable=True),
        sa.Column("edge_terminal_build", sa.Integer(), nullable=True),
        sa.Column("edge_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )


def downgrade() -> None:
    op.drop_table("execution_worker_heartbeats")
