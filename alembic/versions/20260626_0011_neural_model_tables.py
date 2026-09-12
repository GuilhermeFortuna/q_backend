"""neural model registry tables

Revision ID: 20260626_0011
Revises: 20260626_0010
Create Date: 2026-06-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260626_0011"
down_revision: Union[str, Sequence[str], None] = "20260626_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "neural_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_key", name="uq_neural_models_model_key"),
    )
    op.create_index(
        "ix_neural_models_symbol_timeframe",
        "neural_models",
        ["symbol", "timeframe"],
        unique=False,
    )

    op.create_table(
        "neural_model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("model_hash", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="trained",
        ),
        sa.Column("train_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n_latents", sa.Integer(), nullable=False),
        sa.Column("input_features", sa.JSON(), nullable=False),
        sa.Column("hyperparams", sa.JSON(), nullable=False),
        sa.Column("val_metrics", sa.JSON(), nullable=False),
        sa.Column("latent_names", sa.JSON(), nullable=False),
        sa.Column("artifact_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["neural_models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_hash", name="uq_neural_model_versions_model_hash"),
        sa.UniqueConstraint(
            "model_id",
            "version",
            name="uq_neural_model_versions_model_version",
        ),
    )
    op.create_index(
        "ix_neural_model_versions_status",
        "neural_model_versions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_neural_model_versions_model_hash",
        "neural_model_versions",
        ["model_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_neural_model_versions_model_hash", table_name="neural_model_versions")
    op.drop_index("ix_neural_model_versions_status", table_name="neural_model_versions")
    op.drop_table("neural_model_versions")
    op.drop_index("ix_neural_models_symbol_timeframe", table_name="neural_models")
    op.drop_table("neural_models")
