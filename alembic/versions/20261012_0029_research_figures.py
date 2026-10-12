"""Create research figures table.

Revision ID: 20261012_0029
Revises: 20261010_0028
"""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0029"
down_revision = "20261010_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_figures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("collection", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_research_figures_collection_title",
        "research_figures",
        ["collection", "title"],
    )
    op.create_index(
        "ix_research_figures_updated_at",
        "research_figures",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_figures_updated_at", table_name="research_figures")
    op.drop_index("ix_research_figures_collection_title", table_name="research_figures")
    op.drop_table("research_figures")
