"""add is_saved to backtest_runs

Revision ID: 20260609_0002
Revises: 20260607_0001
Create Date: 2026-06-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260609_0002"
down_revision: Union[str, Sequence[str], None] = "20260607_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column(
            "is_saved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index("ix_backtest_runs_is_saved", "backtest_runs", ["is_saved"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_is_saved", table_name="backtest_runs")
    op.drop_column("backtest_runs", "is_saved")
