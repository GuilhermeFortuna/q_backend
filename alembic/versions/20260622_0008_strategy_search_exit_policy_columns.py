"""Add genetic exit-policy metadata columns to strategy_search_candidates.

Revision ID: 20260622_0008
Revises: 20260621_0007
Create Date: 2026-06-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260622_0008"
down_revision: Union[str, Sequence[str], None] = "20260621_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_search_candidates",
        sa.Column("exit_policy_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("exit_policy_label", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("last_exit_mutation_op", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_search_candidates", "last_exit_mutation_op")
    op.drop_column("strategy_search_candidates", "exit_policy_label")
    op.drop_column("strategy_search_candidates", "exit_policy_id")
