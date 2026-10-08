"""add origin and provenance to backtest_runs

Revision ID: 20261008_0027
Revises: 20261003_0026
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0027"
down_revision: Union[str, Sequence[str], None] = "20261003_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="stack"),
    )
    op.add_column("backtest_runs", sa.Column("provenance", sa.JSON(), nullable=True))
    op.create_index("ix_backtest_runs_origin", "backtest_runs", ["origin"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_origin", table_name="backtest_runs")
    op.drop_column("backtest_runs", "provenance")
    op.drop_column("backtest_runs", "origin")
