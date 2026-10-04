"""persist ML filter jobs, models and one-time evaluations

Revision ID: 20261003_0026
Revises: 20260929_0025
Create Date: 2026-10-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261003_0026"
down_revision: Union[str, Sequence[str], None] = "20260929_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ml_filter_runs",
        sa.Column("run_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=True),
        sa.Column("source_run_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("lake_paths", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ml_filter_runs_status", "ml_filter_runs", ["status"])
    op.create_index("ix_ml_filter_runs_type_created", "ml_filter_runs", ["run_type", "created_at"])
    op.create_index("ix_ml_filter_runs_dataset", "ml_filter_runs", ["dataset_id"])

    op.create_table(
        "ml_filter_model_versions",
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", sa.String(length=36), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_version_id"),
        sa.UniqueConstraint(
            "dataset_id", "algorithm", "model_version_id", name="uq_ml_filter_model_dataset_algorithm_version"
        ),
    )
    op.create_index("ix_ml_filter_model_dataset", "ml_filter_model_versions", ["dataset_id"])
    op.create_index("ix_ml_filter_model_algorithm", "ml_filter_model_versions", ["algorithm"])

    op.create_table(
        "ml_filter_evaluations",
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["ml_filter_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_ml_filter_evaluations_model", "ml_filter_evaluations", ["model_version_id"])


def downgrade() -> None:
    op.drop_index("ix_ml_filter_evaluations_model", table_name="ml_filter_evaluations")
    op.drop_table("ml_filter_evaluations")
    op.drop_index("ix_ml_filter_model_algorithm", table_name="ml_filter_model_versions")
    op.drop_index("ix_ml_filter_model_dataset", table_name="ml_filter_model_versions")
    op.drop_table("ml_filter_model_versions")
    op.drop_index("ix_ml_filter_runs_dataset", table_name="ml_filter_runs")
    op.drop_index("ix_ml_filter_runs_type_created", table_name="ml_filter_runs")
    op.drop_index("ix_ml_filter_runs_status", table_name="ml_filter_runs")
    op.drop_table("ml_filter_runs")
