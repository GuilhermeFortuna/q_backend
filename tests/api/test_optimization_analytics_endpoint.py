"""API tests for GET /api/v1/optimize/{study_id}/analytics."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import optuna
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api import optimization_jobs
from q_backend.api.routers.optimization import get_optimization_analytics
from q_backend.optimization.models import OptimizationConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import create_optimization_study


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
def api_session_factory(api_db_engine):
    return sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def api_session_scope(api_session_factory):
    @contextmanager
    def test_session_scope():
        session = api_session_factory()
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
def api_db_session(api_session_factory) -> Session:
    session = api_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _config(*, n_trials: int = 3, study_name: str = "analytics-api") -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": study_name,
                "n_trials": n_trials,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "WIN$",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
                "strategy": "MACrossover",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 8},
                    "long_period": {"type": "int", "low": 20, "high": 40},
                },
            },
        }
    )


def test_analytics_endpoint_404_for_unknown_study():
    with pytest.raises(HTTPException) as exc_info:
        get_optimization_analytics("00000000-0000-0000-0000-000000000099")
    assert exc_info.value.status_code == 404


def test_analytics_finished_study_returns_all_datasets(
    run_jobs_sync, api_session_scope
):
    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 2):
            job = optimization_jobs.start_job(
                _config(n_trials=3, study_name="finished-analytics")
            )

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 2):
            with patch(
                "q_backend.optimization.analytics.get_param_importances",
                return_value={"short_period": 0.6, "long_period": 0.4},
            ):
                payload = get_optimization_analytics(job.study_id)

    assert payload["study_id"] == job.study_id
    assert payload["status"] == "done"
    assert payload["n_complete_trials"] == 3
    assert payload["parallel_coordinate"]["rows"]
    assert payload["pareto_front"]["points"]
    assert payload["param_importances"] is not None
    assert "maximize_net_profit" in payload["param_importances"]


def test_analytics_running_study_returns_partial_payload_without_409(
    api_db_session, api_session_scope, tmp_path
):
    config = _config(n_trials=10, study_name="running-analytics")
    worker_config = config.model_copy(deep=True)
    sqlite_path = tmp_path / "running-analytics.db"
    worker_config.study.storage = worker_config.study.storage.model_copy(
        update={"type": "sqlite", "path": str(sqlite_path)}
    )

    study = optuna.create_study(
        study_name=worker_config.study.name,
        storage=f"sqlite:///{sqlite_path.as_posix()}",
        direction="maximize",
    )
    study.optimize(lambda trial: trial.suggest_float("x", 0, 1), n_trials=2)

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        with api_session_scope() as session:
            db_study = create_optimization_study(
                session,
                name="running-analytics",
                config=worker_config.model_dump(mode="json"),
                status="running",
            )
            study_id = db_study.id.hex

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        payload = get_optimization_analytics(study_id)

    assert payload["status"] == "running"
    assert payload["n_complete_trials"] == 2
    assert payload["parallel_coordinate"]["rows"]
    assert payload["pareto_front"]["points"]
    assert payload["param_importances"] is None


def test_analytics_running_study_does_not_409_when_results_would(
    api_db_session, api_session_scope, tmp_path
):
    config = _config(n_trials=10, study_name="running-vs-results")
    worker_config = config.model_copy(deep=True)
    sqlite_path = tmp_path / "running-vs-results.db"
    worker_config.study.storage = worker_config.study.storage.model_copy(
        update={"type": "sqlite", "path": str(sqlite_path)}
    )

    optuna.create_study(
        study_name=worker_config.study.name,
        storage=f"sqlite:///{sqlite_path.as_posix()}",
        direction="maximize",
    ).optimize(lambda trial: trial.suggest_float("x", 0, 1), n_trials=1)

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        with api_session_scope() as session:
            db_study = create_optimization_study(
                session,
                name="running-vs-results",
                config=worker_config.model_dump(mode="json"),
                status="running",
            )
            study_id = db_study.id.hex

        from q_backend.api.routers.optimization import get_optimization_results

        with pytest.raises(HTTPException) as results_exc:
            get_optimization_results(study_id)
        assert results_exc.value.status_code == 409

        analytics = get_optimization_analytics(study_id)
        assert analytics["status"] == "running"
        assert analytics["param_importances"] is None
