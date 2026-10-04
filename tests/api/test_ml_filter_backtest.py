"""API/worker integration of the MA Crossover · ML Filter backtest (Q-087)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backtest_test_helpers import run_async_backtest
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers.backtest import get_backtest_status, start_backtest
from q_backend.market_data.models import OHLCV
from q_backend.ml_filters.compatibility import (
    MLFilterModelUnavailableError,
    compatibility_fingerprint,
    source_equivalent_config,
)
from q_backend.storage.db.base import Base

MODEL_ID = "a" * 64
CLOSES = [
    100.0, 99.0, 98.0, 97.0, 96.0, 97.5, 99.5, 101.5, 103.5, 105.5, 104.0, 102.0, 100.0, 98.0, 96.0,
    94.0, 95.5, 97.5, 99.5, 101.5, 103.5, 102.0, 100.0, 98.0, 96.0, 94.0, 92.0,
]  # fmt: skip
BODY = {
    "symbol": "WIN$",
    "timeframe": "D1",
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-03-01T00:00:00Z",
    "point_value": 0.2,
    "strategy": "MACrossoverMLFilter",
    "strategy_params": {"short_period": 2, "long_period": 4},
    "ml_filter": {"model_version_id": MODEL_ID, "threshold": 0.5},
}


class _Model:
    feature_names = ("close", "side")

    def __init__(self, reject_closes: tuple[float, ...] = ()) -> None:
        self.reject_closes = reject_closes

    def predict_good_entry_probability(self, X):
        return np.array([0.1 if round(float(c), 4) in self.reject_closes else 0.9 for c in X["close"]])


class _Fitted:
    def __init__(self, classifier) -> None:
        self.classifier = classifier
        self.feature_names = classifier.feature_names

    def predict_good_entry_probability(self, X):
        return self.classifier.predict_good_entry_probability(X)


def _manifest(body: dict, *, train_end: str = "2023-12-01T00:00:00+00:00") -> dict:
    return {
        "model_version_id": MODEL_ID,
        "dataset_id": "b" * 64,
        "algorithm": "logistic_regression",
        "train_end": train_end,
        "compatibility_fingerprint": compatibility_fingerprint(source_equivalent_config(_dump(body))),
    }


def _dump(body: dict) -> dict:
    return BacktestJobRequest.model_validate(body).model_dump(mode="json")


@pytest.fixture
def api_session_scope():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield scope
    engine.dispose()


@pytest.fixture
def sample_ohlcv():
    base = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    bars = []
    previous = CLOSES[0]
    for index, close in enumerate(CLOSES):
        bars.append(
            OHLCV(
                time=base + timedelta(days=index),
                open=previous,
                high=max(previous, close) + 0.5,
                low=min(previous, close) - 0.5,
                close=close,
                tick_volume=100,
            )
        )
        previous = close
    return bars


def _patch_model(body: dict, classifier: _Model, **manifest_overrides):
    manifest = {**_manifest(body), **manifest_overrides}
    return [
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest),
        patch("q_backend.ml_filters.service.resolve_filter_model", return_value=(_Fitted(classifier), manifest)),
    ]


def test_ml_filter_backtest_completes_with_candidate_summary(run_jobs_sync, api_session_scope, sample_ohlcv):
    rejected = (round(CLOSES[11], 4),)  # signal-bar close of the first reversal candidate (bar 11)
    patches = _patch_model(BODY, _Model(reject_closes=rejected))

    run_id, result = run_async_backtest(
        BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
        extra_patches=patches,
    )

    summary = result["ml_filter_summary"]
    assert result["ml_filter"] == BODY["ml_filter"]
    assert summary["model_version_id"] == MODEL_ID
    assert summary["threshold"] == 0.5
    assert summary["candidate_signals"] == summary["scored"] == summary["accepted"] + summary["rejected"]
    assert summary["rejected"] >= 1
    # Candidate signals and executed trades are reported separately.
    assert summary["executed_trades"] == summary["accepted"]
    assert any(item["key"] == "ml_filter_score" for item in result["indicators"])


def test_original_strategy_result_has_no_ml_filter_fields(run_jobs_sync, api_session_scope, sample_ohlcv):
    body = {key: value for key, value in BODY.items() if key != "ml_filter"} | {"strategy": "MACrossover"}
    _run_id, result = run_async_backtest(body, api_session_scope=api_session_scope, sample_ohlcv=sample_ohlcv)

    assert "ml_filter" not in result and "ml_filter_summary" not in result
    assert "ml_filter" not in BacktestJobRequest.model_validate(body).model_dump(mode="json")
    assert all(item["key"] != "ml_filter_score" for item in result["indicators"])


def test_start_rejects_original_strategy_with_filter(run_jobs_sync, api_session_scope):
    body = {**BODY, "strategy": "MACrossover"}
    with patch("q_backend.api.backtest_jobs.session_scope", api_session_scope):
        with pytest.raises(HTTPException) as error:
            start_backtest(BacktestJobRequest.model_validate(body))
    assert error.value.status_code == 422
    assert "cannot be combined" in error.value.detail


def test_start_rejects_variant_without_filter(run_jobs_sync, api_session_scope):
    body = {key: value for key, value in BODY.items() if key != "ml_filter"}
    with patch("q_backend.api.backtest_jobs.session_scope", api_session_scope):
        with pytest.raises(HTTPException) as error:
            start_backtest(BacktestJobRequest.model_validate(body))
    assert error.value.status_code == 422


def test_start_rejects_mismatched_model_with_conflict(run_jobs_sync, api_session_scope):
    manifest = _manifest({**BODY, "point_value": 1.0})
    with (
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
    ):
        with pytest.raises(HTTPException) as error:
            start_backtest(BacktestJobRequest.model_validate(BODY))
    assert error.value.status_code == 409


def test_start_reports_missing_model_as_not_found(run_jobs_sync, api_session_scope):
    with (
        patch(
            "q_backend.ml_filters.service.read_filter_manifest",
            side_effect=MLFilterModelUnavailableError("Ready ML filter model was not found"),
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
    ):
        with pytest.raises(HTTPException) as error:
            start_backtest(BacktestJobRequest.model_validate(BODY))
    assert error.value.status_code == 404


def test_worker_model_failure_persists_a_failed_run(run_jobs_sync, api_session_scope, sample_ohlcv):
    manifest = _manifest(BODY)
    with (
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest),
        patch(
            "q_backend.ml_filters.service.resolve_filter_model",
            side_effect=MLFilterModelUnavailableError("model artifact is corrupt"),
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=__import__("backtest_test_helpers").mock_worker_market_service(ohlcv=sample_ohlcv),
        ),
    ):
        run_id = start_backtest(BacktestJobRequest.model_validate(BODY))["run_id"]
        status = get_backtest_status(run_id)

    assert status["status"] == "failed"
    assert "corrupt" in status["error"]
