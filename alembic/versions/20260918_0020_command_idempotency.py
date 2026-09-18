"""Persist idempotent execution command results.

Revision ID: 20260918_0020
Revises: 20260915_0019
Create Date: 2026-09-18

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260918_0020"
down_revision: Union[str, Sequence[str], None] = "20260915_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "command_idempotency",
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key", name="pk_command_idempotency"),
    )
    op.create_index(
        "ix_command_idempotency_created_at",
        "command_idempotency",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_command_idempotency_created_at", table_name="command_idempotency")
    op.drop_table("command_idempotency")
