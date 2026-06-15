"""Add genetic metadata columns to strategy_search_candidates.

Revision ID: 20260613_0005
Revises: 20260613_0004
Create Date: 2026-06-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0005"
down_revision: Union[str, Sequence[str], None] = "20260613_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_search_candidates",
        sa.Column("generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("genome", sa.JSON(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("genome_node_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("dsr", sa.Float(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("complexity_penalty", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_search_candidates", "complexity_penalty")
    op.drop_column("strategy_search_candidates", "dsr")
    op.drop_column("strategy_search_candidates", "genome_node_count")
    op.drop_column("strategy_search_candidates", "genome")
    op.drop_column("strategy_search_candidates", "generation")
