"""initial storage schema

Revision ID: 20260607_0001
Revises:
Create Date: 2026-06-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260607_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_strategies_name", "strategies", ["name"], unique=False)

    op.create_table(
        "strategy_versions",
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_strategy_version"),
    )

    op.create_table(
        "backtest_configs",
        sa.Column("strategy_version_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backtest_configs_strategy_version_id",
        "backtest_configs",
        ["strategy_version_id"],
        unique=False,
    )

    op.create_table(
        "backtest_runs",
        sa.Column("backtest_config_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lake_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["backtest_config_id"], ["backtest_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"], unique=False)
    op.create_index(
        "ix_backtest_runs_config_created",
        "backtest_runs",
        ["backtest_config_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "optimization_studies",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_optimization_studies_name", "optimization_studies", ["name"], unique=False)
    op.create_index("ix_optimization_studies_status", "optimization_studies", ["status"], unique=False)

    op.create_table(
        "optimization_trials",
        sa.Column("study_id", sa.Uuid(), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["optimization_studies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", "trial_number", name="uq_optimization_trials_study_trial"),
    )

    op.create_table(
        "data_ingestion_runs",
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("dataset_type", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lake_path", sa.String(length=1024), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_data_ingestion_runs_symbol_dataset",
        "data_ingestion_runs",
        ["symbol", "dataset_type"],
        unique=False,
    )
    op.create_index("ix_data_ingestion_runs_status", "data_ingestion_runs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_data_ingestion_runs_status", table_name="data_ingestion_runs")
    op.drop_index("ix_data_ingestion_runs_symbol_dataset", table_name="data_ingestion_runs")
    op.drop_table("data_ingestion_runs")
    op.drop_table("optimization_trials")
    op.drop_index("ix_optimization_studies_status", table_name="optimization_studies")
    op.drop_index("ix_optimization_studies_name", table_name="optimization_studies")
    op.drop_table("optimization_studies")
    op.drop_index("ix_backtest_runs_config_created", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status", table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.drop_index("ix_backtest_configs_strategy_version_id", table_name="backtest_configs")
    op.drop_table("backtest_configs")
    op.drop_table("strategy_versions")
    op.drop_index("ix_strategies_name", table_name="strategies")
    op.drop_table("strategies")
