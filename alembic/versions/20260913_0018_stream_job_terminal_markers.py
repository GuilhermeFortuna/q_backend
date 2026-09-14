"""stream job terminal markers table

Revision ID: 20260913_0018
Revises: 20260912_0017
Create Date: 2026-09-13

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_0018"
down_revision: Union[str, Sequence[str], None] = "20260912_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stream_job_terminal_markers",
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("outbox_seq", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("kind", "job_id", name="pk_stream_job_terminal_markers"),
    )


def downgrade() -> None:
    op.drop_table("stream_job_terminal_markers")
