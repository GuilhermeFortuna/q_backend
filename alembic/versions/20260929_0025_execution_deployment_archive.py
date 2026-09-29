"""add audited deployment archive marker

Revision ID: 20260929_0025
Revises: 20260928_0024
Create Date: 2026-09-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0025"
down_revision: Union[str, Sequence[str], None] = "20260928_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_deployments",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("execution_deployments", "archived")
