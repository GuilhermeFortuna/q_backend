"""Add diagnostics JSON column to strategy_search_candidates.

Revision ID: 20260621_0007
Revises: 20260621_0006
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260621_0007"
down_revision: Union[str, Sequence[str], None] = "20260621_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_search_candidates",
        sa.Column("diagnostics", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_search_candidates", "diagnostics")
