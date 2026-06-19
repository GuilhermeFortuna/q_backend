"""Optimization persistence tests against the Dramatiq dispatch model.

``run_jobs_sync`` runs the coordinator/trial-worker/finalizer chain in-process, so
``start_job`` returns only after the study has finished and been persisted. We then
assert against the database and the public API read functions.
"""

import re
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from q_backend.api import optimization_jobs
from q_backend.api.routers.optimization import (
    bulk_delete_optimizations,
    cancel_optimization,
    delete_optimization,
    get_optimization_results,
    get_optimization_status,
    list_optimizations,
)
from q_backend.api.schemas.common import BulkDeleteOptimizationsRequest
from q_backend.optimization.models import OptimizationConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import create_optimization_study, get_optimization_study


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


def _config(n_trials: int = 3) -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": "WIN$ MA sweep",
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
                "risk_params": {
                    "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                    "quantity": {"type": "float", "low": 1.0, "high": 3.0},
                },
            },
        }
    )


def _start_persisted_job(api_session_scope, n_trials: int = 3):
    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        job = optimization_jobs.start_job(_config(n_trials=n_trials))
    return job


def test_optimization_persists_study_and_trials(
    run_jobs_sync, api_db_session, api_session_scope
):
    job = _start_persisted_job(api_session_scope, n_trials=3)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.study_id)

    study = get_optimization_study(api_db_session, uuid.UUID(hex=job.study_id))
    assert study is not None
    assert study.name == "WIN$ MA sweep"
    assert study.status == "done"
    assert len(study.trials) == 3


def test_optimization_results_rebuild_after_restart(
    run_jobs_sync, api_db_session, api_session_scope
):
    job = _start_persisted_job(api_session_scope, n_trials=3)

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        rebuilt = get_optimization_results(job.study_id)

    assert rebuilt["study_id"] == job.study_id
    assert len(rebuilt["trials"]) == 3
    assert rebuilt["objective_mode"] == "maximize_net_profit"


def test_list_optimizations_returns_persisted_studies(
    run_jobs_sync, api_db_session, api_session_scope
):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    list_payload = list_optimizations(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert len(list_payload["items"]) == 1
    item = list_payload["items"][0]
    assert item.study_id == job.study_id
    assert item.name == "WIN$ MA sweep"
    assert item.status == "done"
    assert item.n_trials == 2
    assert item.completed_trials == 2


def test_optimization_status_rebuild_after_restart(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        status = get_optimization_status(job.study_id)

    assert status["study_id"] == job.study_id
    assert status["status"] == "done"
    assert status["completed_trials"] == 2
    assert status["optimization_config"] is not None
    assert status["optimization_config"]["backtest"]["symbol"] == "WIN$"
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_delete_optimization_removes_study_from_history(
    run_jobs_sync, api_db_session, api_session_scope
):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    delete_optimization(job.study_id, session=api_db_session)

    list_payload = list_optimizations(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert list_payload["items"] == []
    assert optimization_jobs.get_job(job.study_id) is None


def test_bulk_delete_optimizations(run_jobs_sync, api_db_session, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    result = bulk_delete_optimizations(
        BulkDeleteOptimizationsRequest(study_ids=[job.study_id, "bad-id"]),
        session=api_db_session,
    )
    assert result["deleted"] == 1
    assert result["not_found"] == ["bad-id"]

    list_payload = list_optimizations(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0


def test_cancel_orphaned_optimization_study(api_db_session, api_session_scope):
    config = _config(n_trials=250).model_dump(mode="json")
    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        with api_session_scope() as session:
            study = create_optimization_study(
                session,
                name="orphan",
                config=config,
                status="pending",
            )
            study_id = study.id.hex

        payload = cancel_optimization(study_id)

    assert payload["status"] == "cancelled"
    assert payload["completed_trials"] == 0
    assert payload["n_trials"] == 250

    api_db_session.expire_all()
    persisted = get_optimization_study(api_db_session, uuid.UUID(hex=study_id))
    assert persisted is not None
    assert persisted.status == "cancelled"

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        status = get_optimization_status(study_id)
    assert status["status"] == "cancelled"


def test_optimization_graceful_degradation_when_persistence_unavailable(run_jobs_sync):
    config = _config(n_trials=2)
    config.study.max_workers = 2
    with patch(
        "q_backend.api.optimization_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = optimization_jobs.start_job(config)

    assert re.fullmatch(r"[0-9a-f]{32}", job.study_id)
    # With no database, status is served from the Redis progress mirror.
    status = optimization_jobs.get_status_payload(job.study_id)
    assert status is not None
    assert status["status"] == "done"
    assert status["workers"] == 2


@pytest.mark.parametrize("attempt", range(5))
def test_optimization_status_rebuild_shows_terminal_not_running(
    run_jobs_sync, api_session_scope, attempt
):
    config = _config(n_trials=3)
    config.study.max_workers = 2
    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        job = optimization_jobs.start_job(config)

    with patch("q_backend.api.optimization_jobs.session_scope", api_session_scope):
        status = get_optimization_status(job.study_id)

    assert status["status"] == "done"
    assert status["workers"] == 2

    with api_session_scope() as session:
        study = get_optimization_study(session, uuid.UUID(hex=job.study_id))
        assert study is not None
        assert study.status == "done"
