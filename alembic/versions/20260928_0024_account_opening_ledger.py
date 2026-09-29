"""Allow account-level opening ledger entries.

Revision ID: 20260928_0024
Revises: 20260927_0023
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0024"
down_revision: Union[str, Sequence[str], None] = "20260927_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("execution_ledger_entries", "deployment_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.alter_column("execution_ledger_entries", "deployment_id", existing_type=sa.Uuid(), nullable=False)
