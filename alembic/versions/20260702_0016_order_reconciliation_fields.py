"""execution order reconciliation bookkeeping fields

Revision ID: 20260702_0016
Revises: 20260630_0015
Create Date: 2026-07-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260702_0016"
down_revision: Union[str, Sequence[str], None] = "20260630_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_orders",
        sa.Column(
            "reconciliation_attempted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "execution_orders",
        sa.Column("reconciliation_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("reconciled_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("reconciliation_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_orders", "reconciliation_detail")
    op.drop_column("execution_orders", "reconciled_by")
    op.drop_column("execution_orders", "reconciled_at")
    op.drop_column("execution_orders", "reconciliation_error")
    op.drop_column("execution_orders", "reconciliation_attempted_at")
