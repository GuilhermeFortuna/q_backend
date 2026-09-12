"""Tests for neural model status promotion guardrails (WO146)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.neural.promotion import (
    IllegalNeuralStatusTransition,
    promote_neural_model,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import get_neural_model_version, set_neural_model_status


def _train_version(
    db_session,
    lake_root_path,
    *,
    model_key: str,
    symbol: str = "TEST",
    train_end_day: int = 10,
):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, train_end_day, tzinfo=timezone.utc)
    features = tuple(f"feature_{index:02d}" for index in range(1, 7))
    config = default_train_encoder_config(
        symbol=symbol,
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


def test_candidate_to_production_succeeds(db_session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path, model_key="promote_ok")
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )

    updated = promote_neural_model(
        db_session,
        model_hash=version.model_hash,
        target_status=NeuralModelStatus.PRODUCTION.value,
    )
    assert updated.status == NeuralModelStatus.PRODUCTION.value

    refreshed = get_neural_model_version(db_session, version.model_hash)
    assert refreshed is not None
    assert refreshed.status == NeuralModelStatus.PRODUCTION.value


def test_trained_to_production_raises(db_session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path, model_key="promote_skip")

    with pytest.raises(IllegalNeuralStatusTransition):
        promote_neural_model(
            db_session,
            model_hash=version.model_hash,
            target_status=NeuralModelStatus.PRODUCTION.value,
        )


def test_archived_cannot_transition(db_session, lake_root_path) -> None:
    version = _train_version(db_session, lake_root_path, model_key="promote_archived")
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.ARCHIVED.value,
    )

    with pytest.raises(IllegalNeuralStatusTransition):
        promote_neural_model(
            db_session,
            model_hash=version.model_hash,
            target_status=NeuralModelStatus.CANDIDATE.value,
        )


def test_production_demotes_prior_production_same_instrument(db_session, lake_root_path) -> None:
    first = _train_version(db_session, lake_root_path, model_key="promote_first_a", train_end_day=10)
    second = _train_version(db_session, lake_root_path, model_key="promote_first_b", train_end_day=11)

    for version in (first, second):
        set_neural_model_status(
            db_session,
            model_hash=version.model_hash,
            status=NeuralModelStatus.CANDIDATE.value,
        )

    promote_neural_model(
        db_session,
        model_hash=first.model_hash,
        target_status=NeuralModelStatus.PRODUCTION.value,
    )
    promote_neural_model(
        db_session,
        model_hash=second.model_hash,
        target_status=NeuralModelStatus.PRODUCTION.value,
    )

    refreshed_first = get_neural_model_version(db_session, first.model_hash)
    refreshed_second = get_neural_model_version(db_session, second.model_hash)
    assert refreshed_first is not None
    assert refreshed_second is not None
    assert refreshed_first.status == NeuralModelStatus.CANDIDATE.value
    assert refreshed_second.status == NeuralModelStatus.PRODUCTION.value


def test_production_leaves_other_instrument_untouched(db_session, lake_root_path) -> None:
    test_a = _train_version(db_session, lake_root_path, model_key="promote_a", symbol="AAA")
    test_b = _train_version(db_session, lake_root_path, model_key="promote_b", symbol="BBB")

    for version in (test_a, test_b):
        set_neural_model_status(
            db_session,
            model_hash=version.model_hash,
            status=NeuralModelStatus.CANDIDATE.value,
        )
        promote_neural_model(
            db_session,
            model_hash=version.model_hash,
            target_status=NeuralModelStatus.PRODUCTION.value,
        )

    refreshed_a = get_neural_model_version(db_session, test_a.model_hash)
    refreshed_b = get_neural_model_version(db_session, test_b.model_hash)
    assert refreshed_a is not None
    assert refreshed_b is not None
    assert refreshed_a.status == NeuralModelStatus.PRODUCTION.value
    assert refreshed_b.status == NeuralModelStatus.PRODUCTION.value
