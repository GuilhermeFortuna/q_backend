"""execution audit events and deployment pending actions

Revision ID: 20260630_0015
Revises: 20260630_0014
Create Date: 2026-06-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260630_0015"
down_revision: Union[str, Sequence[str], None] = "20260630_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_deployments",
        sa.Column("pending_action", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "execution_deployments",
        sa.Column("pending_action_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "execution_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["execution_deployments.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_audit_events_deployment",
        "execution_audit_events",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_audit_events_type",
        "execution_audit_events",
        ["event_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_audit_events_type", table_name="execution_audit_events")
    op.drop_index("ix_execution_audit_events_deployment", table_name="execution_audit_events")
    op.drop_table("execution_audit_events")
    op.drop_column("execution_deployments", "pending_action_requested_at")
    op.drop_column("execution_deployments", "pending_action")
