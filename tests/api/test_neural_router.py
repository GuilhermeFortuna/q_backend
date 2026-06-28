"""Neural model REST API tests (WO146)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers.neural import (
    get_neural_model,
    get_neural_training_status,
    list_neural_models,
    start_neural_training,
    update_neural_model_status,
)
from q_backend.api.schemas.neural import NeuralModelStatusUpdateRequest, NeuralTrainRequest
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import (
    EvaluationRun,
    FeatureScoreRow,
    NeuralModelStatus,
    RunStatus,
)
from q_backend.storage.db.repositories import set_neural_model_status


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
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    from q_backend.storage.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _train_version(session: Session, lake_root_path, *, model_key: str = "api_pca_h1"):
    import numpy as np
    import pandas as pd

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 10, tzinfo=timezone.utc)
    features = tuple(f"feature_{index:02d}" for index in range(1, 7))
    config = default_train_encoder_config(
        symbol="TEST",
        timeframe="H1",
        train_start=start,
        train_end=end,
        n_latents=2,
        input_features=features,
        model_key=model_key,
    )
    rng = np.random.default_rng(3)
    index = pd.date_range(start, end, freq="h", tz="UTC")
    frame = pd.DataFrame(
        rng.normal(size=(len(index), len(features))),
        index=index,
        columns=features,
    )
    return train_encoder(session, config, feature_window=frame)


def _seed_gate_result(session: Session, version) -> EvaluationRun:
    run = EvaluationRun(
        id=uuid.uuid4(),
        symbol=version.model.symbol,
        timeframe=version.model.timeframe,
        start=datetime(2024, 1, 11, tzinfo=timezone.utc),
        end=datetime(2024, 1, 20, tzinfo=timezone.utc),
        target_name="fwd_return",
        target_horizon=5,
        matrix_id="matrix-test",
        status=RunStatus.COMPLETED.value,
        feature_count=version.n_latents,
        finished_at=datetime(2024, 1, 21, tzinfo=timezone.utc),
    )
    session.add(run)
    session.flush()

    for index, latent_name in enumerate(version.latent_names):
        session.add(
            FeatureScoreRow(
                run_id=run.id,
                feature_id=f"latent-{index}",
                feature_name=f"{latent_name}@{version.model_hash[:8]}",
                ic=0.12 if index == 0 else 0.05,
                rank_ic=0.1,
                mutual_info=0.08,
                stability=0.9,
                global_score=0.11,
                cluster_id=0,
                is_representative=index == 0,
                leakage_status="clean",
                regime_ics={},
            )
        )
    session.flush()
    return run


def test_list_neural_models_returns_trained_versions(
    api_db_session: Session, lake_root_path
) -> None:
    version = _train_version(api_db_session, lake_root_path)
    response = list_neural_models(session=api_db_session, status=None)
    assert len(response["models"]) == 1
    item = response["models"][0]
    assert item.model_hash == version.model_hash
    assert item.status == NeuralModelStatus.TRAINED.value
    assert item.n_latents == 2
    assert item.val_metrics["reconstruction_r2"] is not None


def test_list_neural_models_honors_status_filter(
    api_db_session: Session, lake_root_path
) -> None:
    version = _train_version(api_db_session, lake_root_path, model_key="api_filter")
    set_neural_model_status(
        api_db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )

    candidates = list_neural_models(
        session=api_db_session, status=NeuralModelStatus.CANDIDATE.value
    )
    assert len(candidates["models"]) == 1
    assert candidates["models"][0].model_hash == version.model_hash

    trained = list_neural_models(
        session=api_db_session, status=NeuralModelStatus.TRAINED.value
    )
    assert trained["models"] == []


def test_get_neural_model_returns_detail_and_gate_result(
    api_db_session: Session, lake_root_path
) -> None:
    version = _train_version(api_db_session, lake_root_path, model_key="api_detail")
    _seed_gate_result(api_db_session, version)

    detail = get_neural_model(version.model_hash, session=api_db_session)
    assert detail.model_hash == version.model_hash
    assert detail.latent_names == version.latent_names
    assert detail.val_metrics["reconstruction_r2"] is not None
    assert detail.gate_result is not None
    assert detail.gate_result.best_latent_ic == pytest.approx(0.12)
    assert detail.gate_result.target_name == "fwd_return"
    assert detail.gate_result.target_horizon == 5


def test_get_neural_model_unknown_returns_404(api_db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_neural_model("missing-hash", session=api_db_session)
    assert exc_info.value.status_code == 404


def test_update_neural_model_status_legal_transition(
    api_db_session: Session, lake_root_path
) -> None:
    version = _train_version(api_db_session, lake_root_path, model_key="api_promote")
    set_neural_model_status(
        api_db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )

    updated = update_neural_model_status(
        version.model_hash,
        NeuralModelStatusUpdateRequest(status=NeuralModelStatus.PRODUCTION),
        session=api_db_session,
    )
    assert updated.status == NeuralModelStatus.PRODUCTION.value


def test_update_neural_model_status_illegal_transition_returns_409(
    api_db_session: Session, lake_root_path
) -> None:
    version = _train_version(api_db_session, lake_root_path, model_key="api_illegal")

    with pytest.raises(HTTPException) as exc_info:
        update_neural_model_status(
            version.model_hash,
            NeuralModelStatusUpdateRequest(status=NeuralModelStatus.PRODUCTION),
            session=api_db_session,
        )
    assert exc_info.value.status_code == 409


def test_update_neural_model_status_invalid_status_returns_422() -> None:
    with pytest.raises(ValidationError):
        NeuralModelStatusUpdateRequest(status="not-a-real-status")  # type: ignore[arg-type]


def test_start_neural_training_returns_job_id(
    run_jobs_sync, api_db_session, lake_root_path, monkeypatch, tmp_path
) -> None:
    from contextlib import contextmanager
    from unittest.mock import patch

    import numpy as np
    import pandas as pd
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(
        bind=api_db_session.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    @contextmanager
    def test_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    request = NeuralTrainRequest.model_validate(
        {
            "kind": "pca",
            "symbol": "TEST",
            "timeframe": "H1",
            "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "train_end": datetime(2024, 1, 10, tzinfo=timezone.utc),
            "n_latents": 2,
            "input_features": ["feature_01", "feature_02"],
            "model_key": "router_train_pca",
        }
    )

    index = pd.date_range(request.train_start, request.train_end, freq="h", tz="UTC")
    frame = pd.DataFrame(
        np.random.default_rng(4).normal(size=(len(index), 2)),
        index=index,
        columns=request.input_features,
    )

    with patch("q_backend.api.neural_jobs.session_scope", test_session_scope), patch(
        "q_backend.neural.training_pipeline.build_training_feature_window",
        lambda *_args, **_kwargs: frame,
    ):
        started = start_neural_training(request)

    assert started["job_id"]
    assert started["status"] == "queued"

    status = get_neural_training_status(started["job_id"])
    assert status["status"] == "completed"
    assert status["model_hash"]


def test_get_neural_training_status_unknown_returns_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_neural_training_status("missing-job-id")
    assert exc_info.value.status_code == 404
