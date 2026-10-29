"""Add strategy_version_id and strategy_bundle_hash to execution deployments and revisions.

Revision ID: 20261029_0032
Revises: 20261029_0031
Create Date: 2026-10-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0032"
down_revision: Union[str, Sequence[str], None] = "20261029_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("execution_deployments") as batch:
        batch.add_column(sa.Column("strategy_version_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("strategy_bundle_hash", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_execution_deployments_strategy_version_id",
            "research_strategy_versions",
            ["strategy_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_execution_deployments_strategy_version_id",
        "execution_deployments",
        ["strategy_version_id"],
        unique=False,
    )

    with op.batch_alter_table("execution_deployment_revisions") as batch:
        batch.add_column(sa.Column("strategy_version_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("strategy_bundle_hash", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_execution_deployment_revisions_strategy_version_id",
            "research_strategy_versions",
            ["strategy_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_execution_deployment_revisions_strategy_version_id",
        "execution_deployment_revisions",
        ["strategy_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_deployment_revisions_strategy_version_id",
        table_name="execution_deployment_revisions",
    )
    with op.batch_alter_table("execution_deployment_revisions") as batch:
        batch.drop_constraint(
            "fk_execution_deployment_revisions_strategy_version_id",
            type_="foreignkey",
        )
        batch.drop_column("strategy_bundle_hash")
        batch.drop_column("strategy_version_id")

    op.drop_index(
        "ix_execution_deployments_strategy_version_id",
        table_name="execution_deployments",
    )
    with op.batch_alter_table("execution_deployments") as batch:
        batch.drop_constraint(
            "fk_execution_deployments_strategy_version_id",
            type_="foreignkey",
        )
        batch.drop_column("strategy_bundle_hash")
        batch.drop_column("strategy_version_id")
