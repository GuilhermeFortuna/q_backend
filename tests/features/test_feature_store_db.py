"""Tests for Feature Store DB persistence (WO130)."""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.features.registry import list_feature_specs
from q_backend.features.sync import sync_registry_to_db
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import FeatureDefinition, FeatureStatus, FeatureVersion
from q_backend.storage.db.repositories import (
    increment_feature_usage,
    list_feature_definitions,
    set_feature_status,
)


def _count_rows(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_sync_registry_to_db_creates_definitions_and_versions(db_session: Session) -> None:
    spec_count = len(list_feature_specs())
    assert spec_count > 0

    sync_registry_to_db(db_session)

    definition_count = _count_rows(db_session, FeatureDefinition)
    version_count = _count_rows(db_session, FeatureVersion)
    assert definition_count == spec_count
    assert version_count == spec_count

    rsi = list_feature_definitions(db_session, category="momentum")
    rsi_names = {definition.name for definition in rsi}
    assert "rsi" in rsi_names
    assert "macd" in rsi_names


def test_sync_registry_to_db_is_idempotent(db_session: Session) -> None:
    sync_registry_to_db(db_session)
    first_definitions = _count_rows(db_session, FeatureDefinition)
    first_versions = _count_rows(db_session, FeatureVersion)

    sync_registry_to_db(db_session)

    assert _count_rows(db_session, FeatureDefinition) == first_definitions
    assert _count_rows(db_session, FeatureVersion) == first_versions


def test_sync_preserves_promoted_status(db_session: Session) -> None:
    sync_registry_to_db(db_session)

    set_feature_status(
        db_session,
        name="rsi",
        version=1,
        status=FeatureStatus.PRODUCTION.value,
    )

    sync_registry_to_db(db_session)

    definitions = list_feature_definitions(
        db_session, status=FeatureStatus.PRODUCTION.value
    )
    rsi = next(defn for defn in definitions if defn.name == "rsi")
    assert len(rsi.versions) == 1
    assert rsi.versions[0].status == FeatureStatus.PRODUCTION.value


def test_increment_feature_usage_and_category_filter(db_session: Session) -> None:
    sync_registry_to_db(db_session)

    updated = increment_feature_usage(db_session, name="rsi", n=3)
    assert updated.usage_count == 3

    increment_feature_usage(db_session, name="rsi", n=2)
    rsi = list_feature_definitions(db_session, category="momentum")
    rsi_row = next(defn for defn in rsi if defn.name == "rsi")
    assert rsi_row.usage_count == 5

    trend_only = list_feature_definitions(db_session, category="trend")
    assert all(defn.category == "trend" for defn in trend_only)
    assert not any(defn.name == "rsi" for defn in trend_only)


def test_feature_store_migration_revision_chain() -> None:
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)

    head = script.get_current_head()
    assert head == "20260628_0013"

    feature_revision = script.get_revision("20260626_0010")
    assert feature_revision is not None
    assert feature_revision.down_revision == "20260626_0009"
    assert callable(feature_revision.module.upgrade)
    assert callable(feature_revision.module.downgrade)
