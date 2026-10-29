"""Add immutable strategy file versions and link backtest runs to them.

Revision ID: 20261029_0031
Revises: 20261014_0030
Create Date: 2026-10-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0031"
down_revision: Union[str, Sequence[str], None] = "20261014_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_strategy_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("class_name", sa.String(length=255), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("source_files", sa.JSON(), nullable=False),
        sa.Column("param_specs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("bundle_hash", name="uq_research_strategy_versions_bundle_hash"),
    )
    op.create_index(
        "ix_research_strategy_versions_strategy_id",
        "research_strategy_versions",
        ["strategy_id"],
        unique=False,
    )
    with op.batch_alter_table("backtest_runs") as batch:
        batch.add_column(sa.Column("strategy_version_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_backtest_runs_strategy_version_id",
            "research_strategy_versions",
            ["strategy_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_backtest_runs_strategy_version_id", "backtest_runs", ["strategy_version_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_strategy_version_id", table_name="backtest_runs")
    with op.batch_alter_table("backtest_runs") as batch:
        batch.drop_constraint("fk_backtest_runs_strategy_version_id", type_="foreignkey")
        batch.drop_column("strategy_version_id")
    op.drop_index("ix_research_strategy_versions_strategy_id", table_name="research_strategy_versions")
    op.drop_table("research_strategy_versions")
