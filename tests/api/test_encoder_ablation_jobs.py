"""Encoder ablation job tests (WO155)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from q_backend.api import encoder_ablation_jobs
from q_backend.api.routers.experiments import (
    get_encoder_ablation_status,
    start_encoder_ablation,
)
from q_backend.api.schemas.experiments import EncoderAblationRequest, EncoderConfigSpec
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import get_neural_model_version, list_neural_model_versions
from q_backend.storage.lake.artifacts import read_encoder_ablation_result

_INPUT_FEATURES = ("rsi", "ma", "macd", "realized_vol")
_N_BARS = 600
_INDEX = pd.date_range("2024-01-01", periods=_N_BARS, freq="h", tz="UTC")
_BAR_INDEX = _INDEX.tz_localize(None)


def _synthetic_bars() -> list[OHLCV]:
    steps = np.arange(_N_BARS)
    close = 100.0 + 10.0 * np.sin(steps / 5.0) + 0.05 * steps
    return [
        OHLCV(
            time=_BAR_INDEX[i].to_pydatetime(),
            open=float(close[i]),
            high=float(close[i] + 1.0),
            low=float(close[i] - 1.0),
            close=float(close[i]),
            tick_volume=1000,
        )
        for i in range(_N_BARS)
    ]


@pytest.fixture
def ablation_db_engine():
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
def ablation_session_factory(ablation_db_engine):
    return sessionmaker(
        bind=ablation_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def ablation_session_scope(ablation_session_factory):
    @contextmanager
    def test_session_scope():
        session = ablation_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return test_session_scope


@pytest.fixture
def ablation_db_session(ablation_session_factory) -> Session:
    session = ablation_session_factory()
    from q_backend.features.sync import sync_registry_to_db

    sync_registry_to_db(session)
    session.commit()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    from q_backend.storage.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def stub_ohlcv(monkeypatch):
    bars = _synthetic_bars()

    def _strip_tz(value) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize(None) if ts.tzinfo is not None else ts

    def _read_ohlcv(symbol, timeframe, start_dt, end_dt):
        lo = _strip_tz(start_dt)
        hi = _strip_tz(end_dt)
        return [bar for bar in bars if lo <= pd.Timestamp(bar.time) <= hi]

    monkeypatch.setattr("q_backend.features.matrix.read_ohlcv_fresh", _read_ohlcv)
    monkeypatch.setattr("q_backend.neural.gate.read_ohlcv_fresh", _read_ohlcv)
    return bars


def _ablation_request(**overrides) -> EncoderAblationRequest:
    payload = {
        "symbol": "SYNABL",
        "timeframe": "H1",
        "target": "fwd_return",
        "horizon": 3,
        "train_start": _INDEX[0].to_pydatetime(),
        "train_end": _INDEX[350].to_pydatetime(),
        "n_latents": 2,
        "input_features": list(_INPUT_FEATURES),
        "configs": [
            EncoderConfigSpec(label="pca", encoder_kind="pca"),
            EncoderConfigSpec(label="ae:default", encoder_kind="ae"),
        ],
    }
    payload.update(overrides)
    return EncoderAblationRequest.model_validate(payload)


def _start_ablation(
    run_jobs_sync,
    ablation_session_scope,
    request: EncoderAblationRequest,
):
    with patch(
        "q_backend.api.encoder_ablation_jobs.session_scope",
        ablation_session_scope,
    ):
        return encoder_ablation_jobs.start_encoder_ablation_job(request=request)


def test_encoder_ablation_completes_with_pca_and_ae_rows(
    run_jobs_sync,
    ablation_session_scope,
    ablation_db_session,
    lake_root_path,
    stub_ohlcv,
) -> None:
    request = _ablation_request()
    started = _start_ablation(run_jobs_sync, ablation_session_scope, request)

    payload = encoder_ablation_jobs.get_encoder_ablation_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["progress"] == "2/2"

    result = payload["result"]
    assert result is not None
    assert len(result["rows"]) == 2
    assert result["best_label"] in {"pca", "ae:default"}
    assert result["symbol"] == "SYNABL"
    assert result["target"] == "fwd_return"

    for row in result["rows"]:
        assert row["model_hash"]
        assert np.isfinite(row["recon_r2"])
        if row["gate_error"] is None:
            assert np.isfinite(row["best_latent_ic"])
            assert np.isfinite(row["baseline_ic"])
            assert row["ic_delta_vs_baseline"] == pytest.approx(row["best_latent_ic"] - row["baseline_ic"])

    lake_result = read_encoder_ablation_result(started["job_id"])
    assert lake_result["best_label"] == result["best_label"]

    ablation_db_session.expire_all()
    versions = list_neural_model_versions(ablation_db_session)
    assert len(versions) == 2
    assert all(version.status == NeuralModelStatus.TRAINED.value for version in versions)
    assert all(version.status != NeuralModelStatus.PRODUCTION.value for version in versions)


def test_encoder_ablation_gate_error_row_does_not_abort_other_configs(
    run_jobs_sync,
    ablation_session_scope,
    ablation_db_session,
    lake_root_path,
    stub_ohlcv,
) -> None:
    request = _ablation_request(
        configs=[
            EncoderConfigSpec(label="pca", encoder_kind="pca"),
            EncoderConfigSpec(
                label="pca_no_oos",
                encoder_kind="pca",
                hyperparams={"train_end": _INDEX[_N_BARS - 1].to_pydatetime()},
            ),
        ],
    )
    started = _start_ablation(run_jobs_sync, ablation_session_scope, request)

    payload = encoder_ablation_jobs.get_encoder_ablation_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"

    rows_by_label = {row["label"]: row for row in payload["result"]["rows"]}
    bad_row = rows_by_label["pca_no_oos"]
    good_row = rows_by_label["pca"]

    assert bad_row["gate_error"] is not None
    assert bad_row["model_hash"]
    assert bad_row["passed"] is None or bad_row["passed"] is False
    assert good_row["gate_error"] is None
    assert np.isfinite(good_row["recon_r2"])
    assert np.isfinite(good_row["best_latent_ic"])

    ablation_db_session.expire_all()
    bad_version = get_neural_model_version(ablation_db_session, bad_row["model_hash"])
    assert bad_version is not None
    assert bad_version.status == NeuralModelStatus.TRAINED.value


def test_encoder_ablation_route_smoke(
    run_jobs_sync,
    ablation_session_scope,
    lake_root_path,
    stub_ohlcv,
) -> None:
    request = _ablation_request()
    with patch(
        "q_backend.api.encoder_ablation_jobs.session_scope",
        ablation_session_scope,
    ):
        started = start_encoder_ablation(request)

    assert started["job_id"]
    assert started["status"] == "queued"

    status = get_encoder_ablation_status(started["job_id"])
    assert status["status"] == "completed"
    assert status["result"] is not None


def test_encoder_ablation_unknown_job_returns_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_encoder_ablation_status("missing-job-id")
    assert exc_info.value.status_code == 404


def test_reconcile_orphaned_encoder_ablation_runs(run_jobs_sync) -> None:
    job_id = "orphan-ablation-job"
    encoder_ablation_jobs._persist_progress(
        job_id,
        encoder_ablation_jobs._base_payload(job_id, status="running", progress="1/2"),
    )

    count = encoder_ablation_jobs.reconcile_orphaned_runs()
    assert count == 1

    payload = encoder_ablation_jobs.get_encoder_ablation_status_payload(job_id)
    assert payload is not None
    assert payload["status"] == "failed"
    assert "orphaned" in payload["error"].lower()
