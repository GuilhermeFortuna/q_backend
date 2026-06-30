"""feature evidence rows for profile-scoped admission

Revision ID: 20260628_0013
Revises: 20260628_0012
Create Date: 2026-06-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260628_0013"
down_revision: Union[str, Sequence[str], None] = "20260628_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feature_evidence_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("feature_id", sa.String(length=128), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("feature_name", sa.String(length=255), nullable=False),
        sa.Column("node_kind", sa.String(length=128), nullable=True),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("split_manifest_hash", sa.String(length=128), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("ic", sa.Float(), nullable=True),
        sa.Column("rank_ic", sa.Float(), nullable=True),
        sa.Column("mutual_info", sa.Float(), nullable=True),
        sa.Column("sign_consistency", sa.Float(), nullable=True),
        sa.Column("median_effect", sa.Float(), nullable=True),
        sa.Column("effect_dispersion", sa.Float(), nullable=True),
        sa.Column("n_obs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permutation_null_floor", sa.Float(), nullable=True),
        sa.Column("deflated_score", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rejection_reasons", sa.JSON(), nullable=False),
        sa.Column("leakage_status", sa.String(length=32), nullable=False),
        sa.Column("is_representative", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("cluster_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempted_feature_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("diagnostics_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "profile_version",
            "feature_id",
            "feature_version",
            "target",
            "horizon",
            "split_manifest_hash",
            name="uq_feature_evidence_composite_key",
        ),
    )
    op.create_index(
        "ix_feature_evidence_profile",
        "feature_evidence_rows",
        ["profile_id", "profile_version"],
        unique=False,
    )
    op.create_index(
        "ix_feature_evidence_decision",
        "feature_evidence_rows",
        ["decision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_feature_evidence_decision", table_name="feature_evidence_rows")
    op.drop_index("ix_feature_evidence_profile", table_name="feature_evidence_rows")
    op.drop_table("feature_evidence_rows")
