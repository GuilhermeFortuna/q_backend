"""Neural training job tests (WO147)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from q_backend.api import neural_jobs
from q_backend.api.routers.neural import get_neural_model
from q_backend.api.schemas.neural import NeuralTrainEvaluateRequest, NeuralTrainRequest
from q_backend.neural.gate import LatentGateResult
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_neural_model_version


@pytest.fixture
def job_db_engine():
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
def job_session_factory(job_db_engine):
    return sessionmaker(
        bind=job_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def job_session_scope(job_session_factory):
    @contextmanager
    def test_session_scope():
        session = job_session_factory()
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
def job_db_session(job_session_factory) -> Session:
    session = job_session_factory()
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


def _train_request(**overrides) -> NeuralTrainRequest:
    payload = {
        "kind": "pca",
        "symbol": "TEST",
        "timeframe": "H1",
        "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "train_end": datetime(2024, 1, 10, tzinfo=timezone.utc),
        "n_latents": 2,
        "input_features": ["feature_01", "feature_02", "feature_03"],
        "model_key": "job_test_pca",
    }
    payload.update(overrides)
    return NeuralTrainRequest.model_validate(payload)


def _synthetic_feature_window(request: NeuralTrainRequest) -> pd.DataFrame:
    index = pd.date_range(
        request.train_start,
        request.train_end,
        freq="h",
        tz="UTC",
    )
    rng = np.random.default_rng(11)
    return pd.DataFrame(
        rng.normal(size=(len(index), len(request.input_features))),
        index=index,
        columns=request.input_features,
    )


def _start_job(run_jobs_sync, job_session_scope, request: NeuralTrainRequest):
    with patch("q_backend.api.neural_jobs.session_scope", job_session_scope), patch(
        "q_backend.neural.training_pipeline.build_training_feature_window",
        lambda *_args, **_kwargs: _synthetic_feature_window(request),
    ):
        return neural_jobs.start_training_job(request=request)


def test_pca_training_job_completes_with_model_hash(
    run_jobs_sync, job_session_scope, job_db_session, lake_root_path
) -> None:
    request = _train_request()
    started = _start_job(run_jobs_sync, job_session_scope, request)

    payload = neural_jobs.get_training_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["progress"] == "done"
    assert payload["model_hash"]
    assert payload["val_metrics"]["reconstruction_r2"] is not None

    job_db_session.expire_all()
    version = get_neural_model_version(job_db_session, payload["model_hash"])
    assert version is not None

    detail = get_neural_model(payload["model_hash"], session=job_db_session)
    assert detail.model_hash == payload["model_hash"]


def test_training_job_with_evaluate_returns_gate_summary(
    run_jobs_sync, job_session_scope, lake_root_path, monkeypatch
) -> None:
    request = _train_request(
        model_key="job_gate_pca",
        evaluate=NeuralTrainEvaluateRequest(target="fwd_return", horizon=5),
    )
    gate = LatentGateResult(
        model_hash="placeholder",
        baseline_ic=0.05,
        best_latent_ic=0.12,
        n_latents_beating_baseline=1,
        passed=True,
        evaluation_run_id="run-123",
    )

    def _fake_evaluate(session, version, *, target_name, horizon):
        gate_with_hash = LatentGateResult(
            model_hash=version.model_hash,
            baseline_ic=gate.baseline_ic,
            best_latent_ic=gate.best_latent_ic,
            n_latents_beating_baseline=gate.n_latents_beating_baseline,
            passed=gate.passed,
            evaluation_run_id=gate.evaluation_run_id,
        )
        return gate_with_hash

    monkeypatch.setattr(
        "q_backend.neural.training_pipeline.evaluate_latents",
        _fake_evaluate,
    )

    started = _start_job(run_jobs_sync, job_session_scope, request)
    payload = neural_jobs.get_training_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["gate"] is not None
    assert payload["gate"]["passed"] is True
    assert payload["gate"]["best_latent_ic"] == pytest.approx(0.12)


def test_invalid_training_request_rejected_at_start() -> None:
    with pytest.raises(ValidationError):
        _train_request(
            train_start=datetime(2024, 1, 10, tzinfo=timezone.utc),
            train_end=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )


def test_training_failure_marks_job_failed(
    run_jobs_sync, job_session_scope, lake_root_path, monkeypatch
) -> None:
    request = _train_request(model_key="job_fail_pca")
    monkeypatch.setattr(
        "q_backend.neural.training_pipeline.train_encoder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    started = _start_job(run_jobs_sync, job_session_scope, request)
    payload = neural_jobs.get_training_status_payload(started["job_id"])
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"
