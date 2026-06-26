"""feature store definition and version tables

Revision ID: 20260626_0009
Revises: 20260622_0008
Create Date: 2026-06-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260626_0009"
down_revision: Union[str, Sequence[str], None] = "20260622_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feature_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_feature_definitions_name"),
    )
    op.create_index(
        "ix_feature_definitions_category",
        "feature_definitions",
        ["category"],
        unique=False,
    )

    op.create_table(
        "feature_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="experimental",
        ),
        sa.Column("node_kind", sa.String(length=128), nullable=False),
        sa.Column("param_keys", sa.JSON(), nullable=False),
        sa.Column("default_params", sa.JSON(), nullable=False),
        sa.Column("forward_window", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leakage_status", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["definition_id"], ["feature_definitions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "definition_id",
            "version",
            name="uq_feature_versions_definition_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("feature_versions")
    op.drop_index("ix_feature_definitions_category", table_name="feature_definitions")
    op.drop_table("feature_definitions")
