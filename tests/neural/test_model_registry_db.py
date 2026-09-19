"""Tests for neural model registry DB persistence (WO142)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.neural.sync import sync_neural_models_to_db
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.models import NeuralModel, NeuralModelStatus, NeuralModelVersion
from q_backend.storage.db.repositories import (
    get_neural_model_version,
    list_neural_model_versions,
    set_neural_model_status,
)


def _count_rows(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _train_version(db_session: Session, lake_root_path, model_key: str = "pca_test_h1"):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 10, tzinfo=timezone.utc)
    features = tuple(f"feature_{index:02d}" for index in range(1, 7))
    config = default_train_encoder_config(
        symbol="TEST",
        timeframe="H1",
        train_start=start,
        train_end=end,
        n_latents=3,
        input_features=features,
        model_key=model_key,
    )

    rng = np.random.default_rng(7)
    index = pd.date_range(start, end, freq="h", tz="UTC")
    frame = pd.DataFrame(
        rng.normal(size=(len(index), len(features))),
        index=index,
        columns=features,
    )
    return train_encoder(db_session, config, feature_window=frame)


def test_create_model_and_version_round_trip(db_session: Session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path)

    fetched = get_neural_model_version(db_session, version.model_hash)
    assert fetched is not None
    assert fetched.model_hash == version.model_hash
    assert fetched.n_latents == 3
    assert fetched.val_metrics["reconstruction_r2"] is not None
    assert fetched.val_metrics["explained_variance"] is not None
    assert fetched.artifact_path.startswith("neural_models/")


def test_set_neural_model_status_sticks_across_resync(db_session: Session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path, model_key="pca_status_h1")
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.PRODUCTION.value,
    )

    sync_neural_models_to_db(db_session)

    refreshed = get_neural_model_version(db_session, version.model_hash)
    assert refreshed is not None
    assert refreshed.status == NeuralModelStatus.PRODUCTION.value


def test_sync_neural_models_to_db_is_idempotent(db_session: Session, lake_root_path) -> None:
    _train_version(db_session, lake_root_path, model_key="pca_sync_h1")

    sync_neural_models_to_db(db_session)
    model_count = _count_rows(db_session, NeuralModel)
    version_count = _count_rows(db_session, NeuralModelVersion)

    sync_neural_models_to_db(db_session)

    assert _count_rows(db_session, NeuralModel) == model_count
    assert _count_rows(db_session, NeuralModelVersion) == version_count


def test_list_neural_model_versions_filters_status(db_session: Session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path, model_key="pca_list_h1")
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )

    candidates = list_neural_model_versions(db_session, status=NeuralModelStatus.CANDIDATE.value)
    assert any(row.model_hash == version.model_hash for row in candidates)


def test_neural_model_migration_revision_chain() -> None:
    # Verify the migration graph statically — matches the house pattern in
    # ``test_feature_store_migration_revision_chain``. We deliberately do NOT call
    # ``command.upgrade``/``downgrade`` here: alembic/env.py resolves the URL from
    # ``get_settings().database_url`` (ignoring the alembic config), so executing a
    # downgrade would run against the live Postgres dev DB and drop its neural tables.
    # Actual DDL up/down is exercised by the integration env, not this unit test.
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)

    head = script.get_current_head()
    assert head == "20260918_0021"

    revision = script.get_revision("20260626_0011")
    assert revision is not None
    assert revision.down_revision == "20260626_0010"
    assert callable(revision.module.upgrade)
    assert callable(revision.module.downgrade)
