"""add hypothesis catalog columns to strategy search candidates

Revision ID: 20260628_0012
Revises: 20260626_0011
Create Date: 2026-06-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260628_0012"
down_revision: Union[str, Sequence[str], None] = "20260626_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_search_candidates",
        sa.Column("profile_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("hypothesis_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("hypothesis_rationale", sa.Text(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("hypothesis_required_features", sa.JSON(), nullable=True),
    )
    op.add_column(
        "strategy_search_candidates",
        sa.Column("hypothesis_template_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_search_candidates", "hypothesis_template_hash")
    op.drop_column("strategy_search_candidates", "hypothesis_required_features")
    op.drop_column("strategy_search_candidates", "hypothesis_rationale")
    op.drop_column("strategy_search_candidates", "hypothesis_id")
    op.drop_column("strategy_search_candidates", "profile_version")
