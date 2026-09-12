"""stream outbox and topic state tables

Revision ID: 20260912_0017
Revises: 20260702_0016
Create Date: 2026-09-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from q_contracts.topics import TOPICS

revision: str = "20260912_0017"
down_revision: Union[str, Sequence[str], None] = "20260702_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INITIAL_EPOCH: str = "20260912-00000001"


def upgrade() -> None:
    json_col = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")

    op.create_table(
        "stream_outbox",
        sa.Column("topic", sa.String(length=64), nullable=False),
        sa.Column("epoch", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("producer_id", sa.String(length=128), nullable=False),
        sa.Column("origin_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("routing_key", json_col, nullable=True),
        sa.Column("payload_kind", sa.String(length=64), nullable=False),
        sa.Column("payload_schema", sa.String(length=128), nullable=False),
        sa.Column("payload", json_col, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("topic", "epoch", "seq", name="pk_stream_outbox"),
    )

    op.create_table(
        "stream_outbox_topic_state",
        sa.Column("topic", sa.String(length=64), nullable=False),
        sa.Column("epoch", sa.String(length=64), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_relayed_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("topic", name="pk_stream_outbox_topic_state"),
    )

    durable_topics = sorted(name for name, t in TOPICS.items() if t.topic_class == "durable")
    topic_state = sa.table(
        "stream_outbox_topic_state",
        sa.column("topic", sa.String),
        sa.column("epoch", sa.String),
        sa.column("last_seq", sa.BigInteger),
        sa.column("last_relayed_seq", sa.BigInteger),
    )
    op.bulk_insert(
        topic_state,
        [
            {
                "topic": topic,
                "epoch": INITIAL_EPOCH,
                "last_seq": 0,
                "last_relayed_seq": 0,
            }
            for topic in durable_topics
        ],
    )


def downgrade() -> None:
    op.drop_table("stream_outbox")
    op.drop_table("stream_outbox_topic_state")
