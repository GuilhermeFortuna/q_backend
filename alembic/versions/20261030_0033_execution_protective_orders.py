"""add protective, pending and working-order columns to execution

Revision ID: 20261030_0033
Revises: 20261029_0032
Create Date: 2026-10-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0033"
down_revision: Union[str, Sequence[str], None] = "20261029_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PRICE = sa.Numeric(20, 8)


def upgrade() -> None:
    op.add_column(
        "execution_orders",
        sa.Column("intent_kind", sa.String(16), nullable=False, server_default="entry"),
    )
    op.add_column("execution_orders", sa.Column("price", _PRICE, nullable=True))
    op.add_column("execution_orders", sa.Column("stop_loss", _PRICE, nullable=True))
    op.add_column("execution_orders", sa.Column("take_profit", _PRICE, nullable=True))
    op.add_column(
        "execution_orders",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_orders",
        sa.Column("external_position_id", sa.String(128), nullable=True),
    )
    op.add_column("execution_net_positions", sa.Column("stop_loss", _PRICE, nullable=True))
    op.add_column("execution_net_positions", sa.Column("take_profit", _PRICE, nullable=True))
    op.add_column(
        "execution_net_positions",
        sa.Column("external_position_id", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_net_positions", "external_position_id")
    op.drop_column("execution_net_positions", "take_profit")
    op.drop_column("execution_net_positions", "stop_loss")
    op.drop_column("execution_orders", "external_position_id")
    op.drop_column("execution_orders", "expires_at")
    op.drop_column("execution_orders", "take_profit")
    op.drop_column("execution_orders", "stop_loss")
    op.drop_column("execution_orders", "price")
    op.drop_column("execution_orders", "intent_kind")
