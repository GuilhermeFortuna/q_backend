"""Walk-forward persistence tests against the Dramatiq dispatch model.

``run_jobs_sync`` runs the coordinator/window-worker/finalizer chain in-process, so
``start_job`` returns only after every window has run and the run is persisted. The
window workers use the harness's synthetic OHLCV.
"""

import re
import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from q_backend.api import walkforward_jobs
from q_backend.api.routers.walkforward import (
    delete_walkforward,
    get_walkforward_equity_artifact,
    get_walkforward_results,
    get_walkforward_status,
    list_walkforwards,
    start_walkforward,
)
from q_backend.api.walkforward_jobs import WalkForwardRequest
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_walkforward_run
from q_backend.storage.lake.artifacts import lake_root
from q_backend.storage.settings import get_settings


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


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _request(*, n_trials: int = 2) -> WalkForwardRequest:
    return WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {
                    "name": "WF WIN$ MA",
                    "n_trials": n_trials,
                    "seed": 42,
                    "storage": {"type": "memory"},
                },
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "timeframe": "D1",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-04-30T00:00:00",
                    "initial_capital": 10_000.0,
                    "point_value": 1.0,
                    "strategy": "MACrossover",
                },
                "search_space": {
                    "strategy_params": {
                        "short_period": {"type": "int", "low": 2, "high": 4},
                        "long_period": {"type": "int", "low": 6, "high": 8},
                    },
                    "risk_params": {
                        "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                        "quantity": {"type": "float", "low": 1.0, "high": 1.0},
                    },
                },
            },
            "walkforward": {
                "train_days": 30,
                "test_days": 15,
                "mode": "rolling",
                "min_windows": 2,
            },
        }
    )


def _start_persisted_job(api_session_scope, *, n_trials: int = 2):
    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        return walkforward_jobs.start_job(_request(n_trials=n_trials))


def test_walkforward_end_to_end_persists_db_and_lake(run_jobs_sync, api_db_session, api_session_scope, lake_root_path):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        results = get_walkforward_results(job.run_id)

    assert results["run_id"] == job.run_id
    assert results["status"] == "completed"
    assert len(results["windows"]) >= 2

    run = get_walkforward_run(api_db_session, uuid.UUID(hex=job.run_id))
    assert run is not None
    assert run.name == "WF WIN$ MA"
    assert run.status == "completed"
    assert len(run.windows) >= 2
    assert run.lake_paths is not None
    assert (lake_root_path / run.lake_paths["oos_equity"]).is_file()
    assert (lake_root_path / run.lake_paths["windows"]).is_file()

    equity_payload = get_walkforward_equity_artifact(job.run_id)
    assert equity_payload["run_id"] == job.run_id


def test_walkforward_graceful_degradation_when_persistence_unavailable(run_jobs_sync):
    with patch(
        "q_backend.api.walkforward_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = walkforward_jobs.start_job(_request(n_trials=2))

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    # With no database, status is served from the Redis progress mirror.
    status = walkforward_jobs.get_status_payload(job.run_id)
    assert status is not None
    assert status["status"] == "completed"


def test_walkforward_status_rebuild_after_restart(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        status = get_walkforward_status(job.run_id)

    assert status["run_id"] == job.run_id
    assert status["status"] == "completed"
    assert status["optimization_config"] is not None
    assert status["walkforward_config"] is not None
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_walkforward_results_rebuild_after_restart(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope):
        rebuilt = get_walkforward_results(job.run_id)

    assert rebuilt["run_id"] == job.run_id
    assert len(rebuilt["windows"]) >= 2


def test_walkforward_cancel_skips_windows(run_jobs_sync, api_db_session, api_session_scope):
    # A run cancelled before its windows execute finalizes as cancelled.
    with (
        patch("q_backend.api.walkforward_jobs.session_scope", api_session_scope),
        patch.object(walkforward_jobs, "is_cancelled", lambda *a, **k: True),
    ):
        job = walkforward_jobs.start_job(_request(n_trials=2))
        status = get_walkforward_status(job.run_id)

    assert status["status"] == "cancelled"
    api_db_session.expire_all()
    run = get_walkforward_run(api_db_session, uuid.UUID(hex=job.run_id))
    assert run is not None
    assert run.status == "cancelled"


def test_list_and_delete_walkforward(run_jobs_sync, api_db_session, api_session_scope, lake_root_path):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    list_payload = list_walkforwards(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert list_payload["items"][0].run_id == job.run_id

    delete_walkforward(job.run_id, session=api_db_session)

    list_payload = list_walkforwards(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert walkforward_jobs.get_job(job.run_id) is None
    assert not (lake_root() / "walkforward" / job.run_id).exists()


def test_start_walkforward_returns_422_for_short_range():
    request = WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {"name": "bad", "n_trials": 1, "storage": {"type": "memory"}},
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-02-01T00:00:00",
                    "strategy": "MACrossover",
                },
                "search_space": {},
            },
            "walkforward": {
                "train_days": 30,
                "test_days": 30,
                "min_windows": 2,
            },
        }
    )

    with pytest.raises(HTTPException) as exc:
        start_walkforward(request)
    assert exc.value.status_code == 422


def test_start_walkforward_returns_422_for_tick_engine():
    request = WalkForwardRequest.model_validate(
        {
            "optimization": {
                "study": {"name": "tick", "n_trials": 1, "storage": {"type": "memory"}},
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "WIN$",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-06-01T00:00:00",
                    "engine": "tick",
                },
                "search_space": {},
            },
            "walkforward": {"train_days": 10, "test_days": 5},
        }
    )

    with pytest.raises(HTTPException) as exc:
        start_walkforward(request)
    assert exc.value.status_code == 422
