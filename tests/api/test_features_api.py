"""Feature Store API tests (WO131)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers.features import (
    get_feature_passport,
    list_features,
    update_feature_status,
)
from q_backend.api.schemas.features import FeatureStatusUpdateRequest
from q_backend.features.registry import list_feature_specs
from q_backend.features.sync import sync_registry_to_db
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import FeatureStatus


@pytest.fixture
def api_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_db_session(api_db_engine) -> Session:
    session_factory = sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def seeded_features(api_db_session: Session) -> Session:
    sync_registry_to_db(api_db_session)
    return api_db_session


def test_list_features_returns_seeded_catalog(seeded_features: Session) -> None:
    response = list_features(session=seeded_features, category=None, status=None)
    assert len(response["features"]) == len(list_feature_specs())
    rsi = next(item for item in response["features"] if item.name == "rsi")
    assert rsi.category == "momentum"
    assert rsi.latest_version == 1
    assert rsi.status == FeatureStatus.EXPERIMENTAL.value
    assert rsi.usage_count == 0
    assert rsi.score is None


def test_list_features_category_filter(seeded_features: Session) -> None:
    momentum = list_features(
        session=seeded_features, category="momentum", status=None
    )
    assert momentum["features"]
    assert all(item.category == "momentum" for item in momentum["features"])
    assert any(item.name == "rsi" for item in momentum["features"])
    assert not any(item.name == "ma" for item in momentum["features"])


def test_list_features_status_filter(seeded_features: Session) -> None:
    experimental = list_features(
        session=seeded_features,
        category=None,
        status=FeatureStatus.EXPERIMENTAL.value,
    )
    assert experimental["features"]
    assert all(
        item.status == FeatureStatus.EXPERIMENTAL.value
        for item in experimental["features"]
    )


def test_get_feature_passport_rsi(seeded_features: Session) -> None:
    passport = get_feature_passport("rsi", session=seeded_features)
    assert passport.name == "rsi"
    assert passport.category == "momentum"
    assert passport.usage_count == 0
    assert passport.evaluation_history == []
    assert len(passport.versions) == 1

    version = passport.versions[0]
    assert version.version == 1
    assert version.status == FeatureStatus.EXPERIMENTAL.value
    assert version.node_kind == "ind.rsi"
    assert version.param_keys == ["period"]
    assert version.default_params == {"period": 14}
    assert version.forward_window == 0
    assert version.leakage_status == "clean"
    assert version.provenance == {"source_wo": "WO127", "author": "registry"}


def test_get_feature_passport_unknown_returns_404(seeded_features: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_feature_passport("does-not-exist", session=seeded_features)
    assert exc_info.value.status_code == 404


def test_update_feature_status_round_trips(seeded_features: Session) -> None:
    updated = update_feature_status(
        "rsi",
        1,
        FeatureStatusUpdateRequest(status=FeatureStatus.PRODUCTION),
        session=seeded_features,
    )
    assert updated.versions[0].status == FeatureStatus.PRODUCTION.value

    passport = get_feature_passport("rsi", session=seeded_features)
    assert passport.versions[0].status == FeatureStatus.PRODUCTION.value


def test_update_feature_status_invalid_status_returns_422() -> None:
    with pytest.raises(ValidationError):
        FeatureStatusUpdateRequest(status="not-a-real-status")  # type: ignore[arg-type]


def test_update_feature_status_unknown_feature_returns_404(
    seeded_features: Session,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        update_feature_status(
            "does-not-exist",
            1,
            FeatureStatusUpdateRequest(status=FeatureStatus.CANDIDATE),
            session=seeded_features,
        )
    assert exc_info.value.status_code == 404
