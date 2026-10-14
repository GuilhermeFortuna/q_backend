"""Create research studies table.

Revision ID: 20261014_0030
Revises: 20261012_0029
"""

import sqlalchemy as sa
from alembic import op

revision = "20261014_0030"
down_revision = "20261012_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_studies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("series", sa.JSON(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "timeframe", "name", name="uq_research_studies_identity"),
    )
    op.create_index(
        "ix_research_studies_symbol_updated_at",
        "research_studies",
        ["symbol", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_studies_symbol_updated_at", table_name="research_studies")
    op.drop_table("research_studies")
