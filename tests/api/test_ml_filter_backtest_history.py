"""History, reload and export behavior of ML-filter backtest runs (Q-087)."""

from __future__ import annotations

import csv
import io
from unittest.mock import patch

import pytest

from backtest_test_helpers import mock_worker_market_service, run_async_backtest
from ml_filter_test_helpers import (
    BODY,
    DATASET_ID,
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
from q_backend.api.routers.backtest import export_backtest_market_data, get_backtest, start_backtest
from q_backend.ml_filters.compatibility import MLFilterModelUnavailableError
from q_backend.storage.db.repositories import create_ml_filter_model_version
from q_backend.storage.settings import get_settings


@pytest.fixture
def session_factory():
    with sqlite_session_factory() as factory:
        yield factory


@pytest.fixture
def api_session_scope(session_factory):
    return scope_for(session_factory)


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _register_model(session_factory, *, status: str = "ready") -> None:
    with session_factory() as session:
        row = create_ml_filter_model_version(
            session,
            model_version_id=MODEL_ID,
            dataset_id=DATASET_ID,
            source_run_id="source",
            algorithm="logistic_regression",
            artifact_path="models/path",
            manifest_path="models/path/manifest.json",
            summary={},
        )
        row.status = status
        session.commit()


def _run_filtered(api_session_scope, lake_root_path) -> str:
    run_id, _result = run_async_backtest(
        BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=build_ohlcv(),
        extra_patches=model_patches(BODY, FakeClassifier(reject_closes=(REJECTED_CLOSE,))),
    )
    return run_id


def test_saved_run_reloads_the_exact_pinned_model_and_threshold(
    run_jobs_sync, api_session_scope, session_factory, lake_root_path
):
    run_id = _run_filtered(api_session_scope, lake_root_path)
    _register_model(session_factory)

    with session_factory() as session:
        detail = get_backtest(run_id, session=session)

    assert detail.config["ml_filter"] == BODY["ml_filter"]
    assert detail.strategy == "MACrossoverMLFilter"
    assert (detail.ml_filter.model_version_id, detail.ml_filter.threshold) == (MODEL_ID, 0.5)
    assert detail.ml_filter.available is True


@pytest.mark.parametrize("status", ["deleted", None])
def test_unavailable_reference_is_reported_not_substituted(
    run_jobs_sync, api_session_scope, session_factory, lake_root_path, status
):
    run_id = _run_filtered(api_session_scope, lake_root_path)
    if status is not None:
        _register_model(session_factory, status=status)
    # A newer, ready version for the same dataset must never stand in for the pinned one.
    with session_factory() as session:
        create_ml_filter_model_version(
            session,
            model_version_id="c" * 64,
            dataset_id=DATASET_ID,
            source_run_id="source",
            algorithm="lightgbm",
            artifact_path="models/new",
            manifest_path="models/new/manifest.json",
            summary={},
        )
        session.commit()
        detail = get_backtest(run_id, session=session)

    assert detail.ml_filter.model_version_id == MODEL_ID
    assert detail.ml_filter.available is False
    assert detail.config["ml_filter"]["model_version_id"] == MODEL_ID


def test_summary_separates_candidate_signals_from_executed_trades(
    run_jobs_sync, api_session_scope, session_factory, lake_root_path
):
    run_id = _run_filtered(api_session_scope, lake_root_path)
    with session_factory() as session:
        detail = get_backtest(run_id, session=session)

    summary = detail.result_summary["ml_filter_summary"]
    assert summary["candidate_signals"] == 4
    assert (summary["scored"], summary["accepted"], summary["rejected"], summary["not_ready"]) == (4, 3, 1, 0)
    assert summary["executed_trades"] == 3
    assert summary["model_version_id"] == MODEL_ID and summary["dataset_id"] == DATASET_ID
    assert "total_trades" in detail.result_summary  # ordinary metrics stay alongside the filter summary


def test_original_strategy_runs_gain_no_ml_filter_fields(
    run_jobs_sync, api_session_scope, session_factory, lake_root_path
):
    body = {key: value for key, value in BODY.items() if key != "ml_filter"} | {"strategy": "MACrossover"}
    run_id, _result = run_async_backtest(body, api_session_scope=api_session_scope, sample_ohlcv=build_ohlcv())
    with session_factory() as session:
        detail = get_backtest(run_id, session=session)

    assert "ml_filter" not in detail.config
    assert "ml_filter_summary" not in (detail.result_summary or {})
    assert "ml_filter" not in detail.model_dump(mode="json", exclude_none=False)


def _csv_columns(run_id: str) -> list[str]:
    response = export_backtest_market_data(run_id)
    return list(csv.DictReader(io.StringIO(response.body.decode("utf-8"))).fieldnames)


def test_market_data_export_keeps_existing_columns_and_adds_filter_diagnostics(
    run_jobs_sync, api_session_scope, lake_root_path
):
    original_body = {key: value for key, value in BODY.items() if key != "ml_filter"} | {"strategy": "MACrossover"}
    original_id, _ = run_async_backtest(original_body, api_session_scope=api_session_scope, sample_ohlcv=build_ohlcv())
    filtered_id = _run_filtered(api_session_scope, lake_root_path)

    original, filtered = _csv_columns(original_id), _csv_columns(filtered_id)
    assert not {"ml_filter_score", "ml_filter_accepted"} & set(original)
    assert filtered[: len(original)] == original or set(original) <= set(filtered)
    assert {"ml_filter_score", "ml_filter_accepted"} <= set(filtered)
    assert not [name for name in filtered if name.startswith("q_signal_")]


def test_runtime_failure_persists_a_failed_run(run_jobs_sync, api_session_scope, session_factory, lake_root_path):
    with (
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest_for(BODY)),
        patch(
            "q_backend.ml_filters.service.resolve_filter_model",
            side_effect=MLFilterModelUnavailableError("model artifact is corrupt"),
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=mock_worker_market_service(ohlcv=build_ohlcv()),
        ),
    ):
        run_id = start_backtest(BacktestJobRequest.model_validate(BODY))["run_id"]

    with session_factory() as session:
        detail = get_backtest(run_id, session=session)
    assert detail.status == "failed"
    assert "corrupt" in detail.error_message
    assert detail.ml_filter.model_version_id == MODEL_ID
