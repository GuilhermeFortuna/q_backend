"""Add exit preset metadata columns to strategy_search_candidates.

Revision ID: 20260621_0006
Revises: 20260613_0005
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260621_0006"
down_revision: Union[str, Sequence[str], None] = "20260613_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_search_candidates",
        sa.Column("exit_preset_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("exit_preset_label", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("exit_param_names", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_search_candidates", "exit_param_names")
    op.drop_column("strategy_search_candidates", "exit_preset_label")
    op.drop_column("strategy_search_candidates", "exit_preset_id")
