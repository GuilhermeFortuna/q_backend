"""Discovery (strategy search) persistence tests against the Dramatiq dispatch model.

``run_jobs_sync`` runs the coordinator/candidate-worker/finalizer chain in-process, so
``start_job`` returns only after every candidate has been evaluated and the run is
persisted. Candidate workers use the harness's synthetic OHLCV; the candidate set comes
from ``request.strategies``.
"""

import re
import uuid
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from q_backend.api import strategy_search_jobs
from q_backend.api.routers.strategy_search import (
    cancel_strategy_search,
    delete_strategy_search,
    get_strategy_search_results,
    get_strategy_search_status,
    list_strategy_searches,
    start_strategy_search,
)
from q_backend.optimization.models import (
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.strategy_search import ExitPresetSearchConfig, GateConfig, StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import (
    create_strategy_search_run,
    get_strategy_search_run,
)
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


def _request(*, n_trials: int = 2, strategies: list[str] | None = None) -> StrategySearchConfig:
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
        strategies=strategies or ["MACrossover", "VMA"],
        include_risk_search=False,
        gates=GateConfig(min_completed_windows=1, min_oos_trades=1),
    )


def _start_persisted_job(api_session_scope, *, n_trials: int = 2):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        return strategy_search_jobs.start_job(_request(n_trials=n_trials))


def test_strategy_search_end_to_end_persists_db_and_lake(
    run_jobs_sync, api_db_session, api_session_scope, lake_root_path
):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    assert results["run_id"] == job.run_id
    assert results["status"] == "completed"
    # Both requested strategies are evaluated and persisted as candidates.
    assert len(results["candidates"]) == 2
    assert results["summary"]["candidate_count"] == 2

    run = get_strategy_search_run(api_db_session, uuid.UUID(hex=job.run_id))
    assert run is not None
    assert run.name == "Discovery WIN$ sweep"
    assert run.status == "completed"
    assert len(run.candidates) == 2
    assert run.lake_paths is not None
    assert (lake_root_path / run.lake_paths["leaderboard"]).is_file()


def test_strategy_search_graceful_degradation_when_persistence_unavailable(run_jobs_sync):
    with patch(
        "q_backend.api.strategy_search_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = strategy_search_jobs.start_job(_request(n_trials=2))

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    status = strategy_search_jobs.get_status_payload(job.run_id)
    assert status is not None
    assert status["status"] == "completed"


def test_strategy_search_graceful_degradation_when_lake_unwritable(run_jobs_sync, monkeypatch):
    monkeypatch.setattr(strategy_search_jobs, "_write_lake_artifacts", lambda *_a, **_k: None)
    job = strategy_search_jobs.start_job(_request(n_trials=2))
    status = strategy_search_jobs.get_status_payload(job.run_id)
    assert status is not None
    assert status["status"] == "completed"


def test_strategy_search_status_rebuild_after_restart(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        status = get_strategy_search_status(job.run_id)

    assert status["run_id"] == job.run_id
    assert status["status"] == "completed"
    assert status["search_config"] is not None
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_strategy_search_results_rebuild_after_restart(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        rebuilt = get_strategy_search_results(job.run_id)

    assert rebuilt["run_id"] == job.run_id
    assert len(rebuilt["candidates"]) == 2


def test_strategy_search_cancel_skips_candidates(run_jobs_sync, api_db_session, api_session_scope):
    # A run cancelled before its candidates execute finalizes as cancelled.
    with (
        patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope),
        patch.object(strategy_search_jobs, "is_cancelled", lambda *a, **k: True),
    ):
        job = strategy_search_jobs.start_job(_request(n_trials=2))
        status = get_strategy_search_status(job.run_id)

    assert status["status"] == "cancelled"
    api_db_session.expire_all()
    run = get_strategy_search_run(api_db_session, uuid.UUID(hex=job.run_id))
    assert run is not None
    assert run.status == "cancelled"


def test_cancel_orphaned_strategy_search_run(api_db_session, api_session_scope, run_jobs_sync):
    # A run left "running" in the DB by a previous process: a DB row exists but no
    # worker is processing it.
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        with api_session_scope() as session:
            run = create_strategy_search_run(
                session,
                name="orphan",
                config=_request().model_dump(mode="json"),
                status="running",
            )
            run_id = run.id.hex

        assert strategy_search_jobs.get_job(run_id) is None
        payload = cancel_strategy_search(run_id)

    assert payload["status"] == "cancelled"

    api_db_session.expire_all()
    persisted = get_strategy_search_run(api_db_session, uuid.UUID(hex=run_id))
    assert persisted.status == "cancelled"
    assert persisted.error_message is not None


def test_reconcile_orphaned_strategy_search_runs(api_db_session, api_session_scope):
    config = _request().model_dump(mode="json")
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        with api_session_scope() as session:
            running = create_strategy_search_run(session, name="r", config=config, status="running")
            pending = create_strategy_search_run(session, name="p", config=config, status="pending")
            done = create_strategy_search_run(session, name="d", config=config, status="completed")
            running_id, pending_id, done_id = running.id, pending.id, done.id

        count = strategy_search_jobs.reconcile_orphaned_runs()

    assert count == 2
    api_db_session.expire_all()
    assert get_strategy_search_run(api_db_session, running_id).status == "cancelled"
    assert get_strategy_search_run(api_db_session, pending_id).status == "cancelled"
    assert get_strategy_search_run(api_db_session, done_id).status == "completed"


def test_list_and_delete_strategy_search(run_jobs_sync, api_db_session, api_session_scope, lake_root_path):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    list_payload = list_strategy_searches(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert list_payload["items"][0].run_id == job.run_id

    delete_strategy_search(job.run_id, session=api_db_session)

    list_payload = list_strategy_searches(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert strategy_search_jobs.get_job(job.run_id) is None
    assert not (lake_root() / "strategy_search" / job.run_id).exists()


def test_start_strategy_search_returns_422_for_short_range():
    request = _request(n_trials=1)
    request = request.model_copy(
        update={
            "backtest": request.backtest.model_copy(update={"end": datetime(2024, 2, 1)}),
            "walkforward": request.walkforward.model_copy(update={"train_days": 30, "test_days": 30, "min_windows": 2}),
        }
    )

    with pytest.raises(HTTPException) as exc:
        start_strategy_search(request)
    assert exc.value.status_code == 422


def test_start_strategy_search_returns_422_for_multi_objective():
    with pytest.raises(ValueError, match="single-objective"):
        StrategySearchConfig(
            backtest={
                "symbol": "WIN$",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            objective=ObjectiveConfig(mode=ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN),
            walkforward=WalkForwardConfig(train_days=10, test_days=5),
            study=StudyConfig(name="multi", n_trials=1),
        )


def test_strategy_search_migration_revision_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = script.get_revision("20260613_0004")
    assert revision is not None
    assert revision.down_revision == "20260611_0003"
    assert callable(revision.module.upgrade)
    assert callable(revision.module.downgrade)

    exit_preset_revision = script.get_revision("20260621_0006")
    assert exit_preset_revision is not None
    assert exit_preset_revision.down_revision == "20260613_0005"

    diagnostics_revision = script.get_revision("20260621_0007")
    assert diagnostics_revision is not None
    assert diagnostics_revision.down_revision == "20260621_0006"

    exit_policy_revision = script.get_revision("20260622_0008")
    assert exit_policy_revision is not None
    assert exit_policy_revision.down_revision == "20260621_0007"


def test_strategy_search_exit_quality_persists_and_rebuilds(
    run_jobs_sync, api_db_session, api_session_scope, lake_root_path
):
    job = _start_persisted_job(api_session_scope, n_trials=2)

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    for candidate in results["candidates"]:
        if candidate["status"] != "completed":
            continue
        assert candidate.get("exit_quality") is not None
        assert candidate["exit_quality"]["total_closed_trades"] >= 0
        assert isinstance(candidate["exit_quality"].get("by_reason"), dict)

    api_db_session.expire_all()
    run = get_strategy_search_run(api_db_session, uuid.UUID(hex=job.run_id))
    completed = [item for item in run.candidates if item.status == "completed" and item.diagnostics]
    assert completed, "expected at least one completed candidate with diagnostics"
    persisted = completed[0]
    assert persisted.diagnostics is not None
    assert persisted.diagnostics.get("exit_quality") is not None

    trades_path = (
        lake_root_path / "strategy_search" / job.run_id / "candidates" / persisted.candidate_id / "oos_trades.parquet"
    )
    assert trades_path.is_file()


def test_strategy_search_old_candidate_without_exit_quality_serializes(api_db_session, api_session_scope):
    from q_backend.storage.db.repositories import create_strategy_search_candidate

    with api_session_scope() as session:
        run = create_strategy_search_run(
            session,
            name="legacy",
            config=_request().model_dump(mode="json"),
            status="completed",
        )
        create_strategy_search_candidate(
            session,
            run_id=run.id,
            candidate_id="MACrossover",
            strategy="MACrossover",
            status="completed",
            rank=1,
            objective_value=100.0,
            robustness_score=100.0,
            passed_gates=True,
        )
        run_id = run.id.hex

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(run_id)

    candidate = results["candidates"][0]
    assert candidate["candidate_id"] == "MACrossover"
    assert candidate.get("diagnostics") is None
    assert candidate.get("exit_quality") is None


def test_strategy_search_old_candidate_without_exit_policy_metadata_serializes(api_db_session, api_session_scope):
    from q_backend.storage.db.repositories import create_strategy_search_candidate

    with api_session_scope() as session:
        run = create_strategy_search_run(
            session,
            name="legacy-genetic",
            config=_request().model_dump(mode="json"),
            status="completed",
        )
        create_strategy_search_candidate(
            session,
            run_id=run.id,
            candidate_id="genome-legacy",
            strategy="CompositeStrategy",
            status="completed",
            rank=1,
            objective_value=100.0,
            robustness_score=100.0,
            passed_gates=True,
            generation=0,
        )
        run_id = run.id.hex

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(run_id)

    candidate = results["candidates"][0]
    assert candidate["candidate_id"] == "genome-legacy"
    assert candidate.get("exit_policy_id") is None
    assert candidate.get("exit_policy_label") is None
    assert candidate.get("last_exit_mutation_op") is None


def test_strategy_search_exit_preset_metadata_persists(run_jobs_sync, api_db_session, api_session_scope):
    request = _request(n_trials=2, strategies=["MACrossover"]).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=["fixed_pct_bracket"],
                include_baseline=False,
            )
        }
    )
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(request)
        results = get_strategy_search_results(job.run_id)

    assert len(results["candidates"]) == 1
    candidate = results["candidates"][0]
    assert candidate["candidate_id"] == "MACrossover__exit_fixed_pct_bracket"
    assert candidate["exit_preset_id"] == "fixed_pct_bracket"
    assert candidate["exit_preset_label"] == "Fixed % bracket"
    assert candidate["exit_param_names"] == ["stop_loss_pct", "take_profit_pct"]

    api_db_session.expire_all()
    run = get_strategy_search_run(api_db_session, uuid.UUID(hex=job.run_id))
    persisted = run.candidates[0]
    assert persisted.exit_preset_id == "fixed_pct_bracket"
    assert persisted.exit_param_names == ["stop_loss_pct", "take_profit_pct"]


def test_strategy_search_results_without_exit_metadata(run_jobs_sync, api_session_scope):
    job = _start_persisted_job(api_session_scope, n_trials=2)
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    for candidate in results["candidates"]:
        assert "exit_preset_id" not in candidate or candidate.get("exit_preset_id") is None
        assert "exit_preset_label" not in candidate or candidate.get("exit_preset_label") is None
        assert "exit_param_names" not in candidate or candidate.get("exit_param_names") is None


def test_strategy_search_status_response_preserves_trial_logs():
    from q_backend.api.schemas.strategy_search import StrategySearchStatusResponse

    sample_log = (
        "[I 2026-06-15 07:37:19,667] Candidate genome-001 - Window 0 - "
        "Trial 1 finished with value: -0.645 and parameters: {'period': 27}. "
        "Best is trial 0 with value: -0.645."
    )
    payload = {
        "run_id": "a" * 32,
        "status": "running",
        "current_candidate": 36.25,
        "total_candidates": 144,
        "logs": [sample_log],
    }

    response = StrategySearchStatusResponse.model_validate(payload)
    assert response.logs == [sample_log]
    assert response.current_candidate == 36.25
