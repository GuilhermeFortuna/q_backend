"""add timed exit instant to open execution positions

Revision ID: 20261030_0034
Revises: 20261030_0033
Create Date: 2026-10-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0034"
down_revision: Union[str, Sequence[str], None] = "20261030_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_net_positions",
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_net_positions", "exit_at")
