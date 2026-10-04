"""API/worker integration of the MA Crossover · ML Filter backtest (Q-087)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backtest_test_helpers import mock_worker_market_service, run_async_backtest
from ml_filter_test_helpers import (
    BODY,
    MODEL_ID,
    REJECTED_CLOSE,
    FakeClassifier,
    build_ohlcv,
    manifest_for,
    model_patches,
    scope_for,
    sqlite_session_factory,
)
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers.backtest import get_backtest_status, start_backtest
from q_backend.ml_filters.compatibility import MLFilterModelUnavailableError


@pytest.fixture
def api_session_scope():
    with sqlite_session_factory() as factory:
        yield scope_for(factory)


@pytest.fixture
def sample_ohlcv():
    return build_ohlcv()


def test_ml_filter_backtest_completes_with_candidate_summary(run_jobs_sync, api_session_scope, sample_ohlcv):
    patches = model_patches(BODY, FakeClassifier(reject_closes=(REJECTED_CLOSE,)))

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
    manifest = manifest_for({**BODY, "point_value": 1.0})
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
    manifest = manifest_for(BODY)
    with (
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest),
        patch(
            "q_backend.ml_filters.service.resolve_filter_model",
            side_effect=MLFilterModelUnavailableError("model artifact is corrupt"),
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=mock_worker_market_service(ohlcv=sample_ohlcv),
        ),
    ):
        run_id = start_backtest(BacktestJobRequest.model_validate(BODY))["run_id"]
        status = get_backtest_status(run_id)

    assert status["status"] == "failed"
    assert "corrupt" in status["error"]
