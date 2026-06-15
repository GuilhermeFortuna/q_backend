"""Genetic strategy search persistence, API, and backward-compat tests (WO40)."""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import q_backend.backtesting.strategies  # noqa: F401 — register CompositeStrategy

from q_backend.api import strategy_search_jobs
from q_backend.api.main import (
    get_strategy_search_candidate_genome,
    get_strategy_search_results,
    get_strategy_search_status,
)
from q_backend.optimization.models import (
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    GateConfig,
    GeneticSearchConfig,
    LockboxConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_strategy_search_run
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


def _registry_request(*, n_trials: int = 2) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "WIN$",
            "timeframe": "D1",
            "start": datetime(2024, 1, 1).isoformat(),
            "end": datetime(2024, 4, 30).isoformat(),
            "initial_capital": 10_000.0,
            "point_value": 1.0,
            "strategy": "MACrossover",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(
            train_days=30,
            test_days=15,
            mode="rolling",
            min_windows=2,
        ),
        study=StudyConfig(
            name="Discovery WIN$ sweep",
            n_trials=n_trials,
            seed=42,
            storage={"type": "memory"},
        ),
        strategies=["MACrossover", "VMA"],
        include_risk_search=False,
        gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
    )


def _genetic_request(*, n_trials: int = 2) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "WIN$",
            "timeframe": "D1",
            "start": datetime(2024, 1, 1).isoformat(),
            "end": datetime(2024, 4, 30).isoformat(),
            "initial_capital": 10_000.0,
            "point_value": 1.0,
            "strategy": "CompositeStrategy",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(
            train_days=30,
            test_days=15,
            mode="rolling",
            min_windows=2,
        ),
        study=StudyConfig(
            name="Genetic WIN$ search",
            n_trials=n_trials,
            seed=42,
            storage={"type": "memory"},
        ),
        include_risk_search=False,
        gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
        genetic=GeneticSearchConfig(
            population_size=10,
            generations=2,
            elite_count=1,
            init_seed=99,
            crossover_rate=0.7,
            mutation_rate=0.2,
            tournament_size=2,
            max_nodes=12,
            max_depth=8,
        ),
        lockbox=LockboxConfig(enabled=True, lockbox_pct=0.15, min_trades=1),
    )


REGISTRY_RESULTS_SNAPSHOT_KEYS = {
    "run_id",
    "status",
    "objective_mode",
    "summary",
    "candidates",
    "best",
    "search_config",
    "lake_paths",
}

REGISTRY_SUMMARY_KEYS = {
    "objective_mode",
    "candidate_count",
    "ranked_count",
    "passed_gates_count",
    "best_candidate_id",
    "best_strategy",
    "best_objective_value",
    "best_efficiency",
}


def test_registry_sweep_backward_compatible_summary(
    run_jobs_sync, api_session_scope, lake_root_path
):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(_registry_request(n_trials=2))

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    assert set(results.keys()) == REGISTRY_RESULTS_SNAPSHOT_KEYS
    assert set(results["summary"].keys()) == REGISTRY_SUMMARY_KEYS
    assert "provider" not in results["search_config"]
    assert "genetic" not in results["search_config"]
    assert "lockbox" not in results["search_config"]


def test_genetic_run_persists_metadata(
    run_jobs_sync, api_db_session, api_session_scope, lake_root_path
):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(_genetic_request(n_trials=2))

    api_db_session.expire_all()
    run = get_strategy_search_run(api_db_session, uuid.UUID(hex=job.run_id))
    assert run is not None
    assert run.config.get("provider") == "genetic"
    summary = run.result_summary or {}
    assert summary.get("generations_completed") == 2
    assert summary.get("total_genomes_evaluated") == 20
    assert "champion_dsr" in summary
    assert "lockbox_passed" in summary

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    assert results["summary"]["generations_completed"] == 2
    assert len(run.candidates) == 20
    assert all(candidate.genome is not None for candidate in run.candidates)
    assert len({candidate.candidate_id for candidate in run.candidates}) == 20


def test_genetic_status_forwards_generation_fields(run_jobs_sync, api_session_scope):
    captured: list[dict] = []

    original = strategy_search_jobs._persist_progress

    def capture(job):
        captured.append(strategy_search_jobs.status_payload(job))
        original(job)

    with (
        patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope),
        patch("q_backend.api.strategy_search_jobs._persist_progress", capture),
    ):
        job = strategy_search_jobs.start_job(_genetic_request(n_trials=1))

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        status = get_strategy_search_status(job.run_id)

    assert status["status"] == "completed"
    assert any(item.get("total_generations") == 2 for item in captured)


def test_genetic_graceful_degradation_postgres_stopped(run_jobs_sync):
    with patch(
        "q_backend.api.strategy_search_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = strategy_search_jobs.start_job(_genetic_request(n_trials=1))

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    status = strategy_search_jobs.get_status_payload(job.run_id)
    assert status is not None
    assert status["status"] == "completed"


def test_genetic_graceful_degradation_lake_unwritable(run_jobs_sync, monkeypatch):
    monkeypatch.setattr(
        strategy_search_jobs, "_write_lake_artifacts", lambda *_a, **_k: None
    )
    job = strategy_search_jobs.start_job(_genetic_request(n_trials=1))
    status = strategy_search_jobs.get_status_payload(job.run_id)
    assert status is not None
    assert status["status"] == "completed"


def test_genome_endpoint_returns_stored_genome(
    run_jobs_sync, api_session_scope, lake_root_path
):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(_genetic_request(n_trials=1))
        results = get_strategy_search_results(job.run_id)

    candidate_id = results["candidates"][0]["candidate_id"]
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        payload = get_strategy_search_candidate_genome(job.run_id, candidate_id)

    assert payload["candidate_id"] == candidate_id
    assert payload["genome"]["version"] == 1


def test_genome_endpoint_404_for_registry_candidate(run_jobs_sync, api_session_scope):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(_registry_request(n_trials=2))
        results = get_strategy_search_results(job.run_id)

    candidate_id = results["candidates"][0]["candidate_id"]
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        with pytest.raises(HTTPException) as exc:
            get_strategy_search_candidate_genome(job.run_id, candidate_id)
    assert exc.value.status_code == 404


def test_genome_endpoint_404_for_unknown_candidate(run_jobs_sync, api_session_scope):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(_genetic_request(n_trials=1))

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        with pytest.raises(HTTPException) as exc:
            get_strategy_search_candidate_genome(job.run_id, "missing-id")
    assert exc.value.status_code == 404
