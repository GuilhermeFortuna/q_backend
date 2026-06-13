import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api import strategy_search_jobs
from q_backend.api.main import (
    cancel_strategy_search,
    delete_strategy_search,
    get_strategy_search_candidate_equity_artifact,
    get_strategy_search_results,
    get_strategy_search_status,
    list_strategy_searches,
    start_strategy_search,
)
from q_backend.optimization.auto_search_space import derive_strategy_search_space
from q_backend.optimization.backtest_runner import BacktestRunConfig, DefaultBacktestRunner
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    ObjectiveConfig,
    ObjectiveMode,
    SearchSpaceConfig,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GateConfig,
    SearchCandidate,
    StrategySearchConfig,
    StrategySearchRunner,
)
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_strategy_search_run
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


@pytest.fixture(autouse=True)
def clear_jobs():
    strategy_search_jobs._jobs.clear()
    yield
    strategy_search_jobs._jobs.clear()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _make_intraday_ohlcv_df(start: datetime, days: int) -> pd.DataFrame:
    rows = []
    price = 100.0
    for day in range(days):
        for hour in (0, 6, 12, 18):
            timestamp = start + timedelta(days=day, hours=hour)
            drift = 0.1 if day % 10 < 5 else -0.05
            price = max(50.0, price + drift)
            rows.append(
                {
                    "time": timestamp,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 1000,
                }
            )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def _sliced_data_provider(full_df: pd.DataFrame):
    def data_provider(config: BacktestRunConfig) -> pd.DataFrame:
        return full_df.loc[config.start : config.end]

    return data_provider


def _narrow_risk_space() -> dict:
    return {
        "type": CategoricalParam(type="categorical", choices=["fixed_quantity"]),
        "quantity": FloatParam(type="float", low=1.0, high=1.0),
    }


def _narrow_search_space(strategy: str) -> SearchSpaceConfig:
    if strategy == "MACrossover":
        return SearchSpaceConfig(
            strategy_params={
                "short_period": IntParam(type="int", low=2, high=4),
                "long_period": IntParam(type="int", low=6, high=8),
            },
            risk_params=_narrow_risk_space(),
        )
    if strategy == "VMA":
        return SearchSpaceConfig(
            strategy_params={
                "period": IntParam(type="int", low=2, high=8),
                "band_pct": FloatParam(type="float", low=0.0, high=0.5, step=0.1),
            },
            risk_params=_narrow_risk_space(),
        )
    raise ValueError(f"No narrow search space fixture for {strategy}")


class NarrowCandidateProvider:
    def __init__(self, strategies: list[str]) -> None:
        self._strategies = strategies

    def candidates(self):
        for name in self._strategies:
            _, fixed = derive_strategy_search_space(name)
            yield SearchCandidate(
                candidate_id=name,
                strategy=name,
                search_space=_narrow_search_space(name),
                fixed_params=fixed,
            )

    def report(self, results: list[CandidateResult]) -> None:
        return None


def _request(*, n_trials: int = 2, strategies: list[str] | None = None) -> StrategySearchConfig:
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    return StrategySearchConfig(
        backtest={
            "symbol": "WIN$",
            "timeframe": "D1",
            "start": start.isoformat(),
            "end": end.isoformat(),
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
            max_workers=1,
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


def _backtest_runner() -> DefaultBacktestRunner:
    start = datetime(2024, 1, 1)
    full_df = _make_intraday_ohlcv_df(start, 120)
    return DefaultBacktestRunner(data_provider=_sliced_data_provider(full_df))


def _provider() -> NarrowCandidateProvider:
    return NarrowCandidateProvider(["MACrossover", "VMA"])


def _wait_for(run_id: str, statuses: set[str], timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = strategy_search_jobs.get_job(run_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"strategy search run {run_id} did not reach {statuses} in time")


def _start_persisted_job(api_session_scope, *, n_trials: int = 2):
    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        job = strategy_search_jobs.start_job(
            _request(n_trials=n_trials),
            backtest_runner=_backtest_runner(),
            provider=_provider(),
        )
        done = _wait_for(job.run_id, {"completed"})
    return job, done


def test_strategy_search_end_to_end_persists_db_and_lake(
    api_db_session, api_session_scope, lake_root_path
):
    job, done = _start_persisted_job(api_session_scope, n_trials=2)
    api_db_session.expire_all()

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    assert done.status == "completed"
    assert done.result is not None
    assert done.result.best is not None

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        results = get_strategy_search_results(job.run_id)

    assert results["run_id"] == job.run_id
    assert len(results["candidates"]) == 2
    assert results["best"] is not None
    assert results["summary"]["candidate_count"] == 2

    run_uuid = uuid.UUID(hex=job.run_id)
    run = get_strategy_search_run(api_db_session, run_uuid)
    assert run is not None
    assert run.name == "Discovery WIN$ sweep"
    assert run.status == "completed"
    assert len(run.candidates) == 2
    assert run.lake_paths is not None
    assert (lake_root_path / run.lake_paths["leaderboard"]).is_file()
    best_id = results["best"]["candidate_id"]
    assert (
        lake_root_path
        / run.lake_paths["candidates"][best_id]["oos_equity"]
    ).is_file()

    equity_payload = get_strategy_search_candidate_equity_artifact(
        job.run_id, best_id
    )
    assert equity_payload["run_id"] == job.run_id
    assert equity_payload["candidate_id"] == best_id
    assert len(equity_payload["points"]) >= 1


def test_strategy_search_graceful_degradation_when_persistence_unavailable():
    with patch(
        "q_backend.api.strategy_search_jobs.session_scope",
        side_effect=Exception("database unavailable"),
    ):
        job = strategy_search_jobs.start_job(
            _request(n_trials=2),
            backtest_runner=_backtest_runner(),
            provider=_provider(),
        )

    assert re.fullmatch(r"[0-9a-f]{32}", job.run_id)
    done = _wait_for(job.run_id, {"completed"})
    assert done.db_run_id is None
    assert strategy_search_jobs.results_payload(done) is not None


def test_strategy_search_graceful_degradation_when_lake_unwritable(monkeypatch):
    monkeypatch.setattr(strategy_search_jobs, "_write_lake_artifacts", lambda *_a, **_k: None)
    job = strategy_search_jobs.start_job(
        _request(n_trials=2),
        backtest_runner=_backtest_runner(),
        provider=_provider(),
    )
    done = _wait_for(job.run_id, {"completed"})
    assert done.lake_paths is None
    assert strategy_search_jobs.results_payload(done) is not None


def test_strategy_search_status_rebuild_after_restart(api_session_scope):
    job, _done = _start_persisted_job(api_session_scope, n_trials=2)
    strategy_search_jobs._jobs.clear()

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        status = get_strategy_search_status(job.run_id)

    assert status["run_id"] == job.run_id
    assert status["status"] == "completed"
    assert status["search_config"] is not None
    assert status["backtest_config"]["symbol"] == "WIN$"


def test_strategy_search_results_rebuild_after_restart(api_session_scope):
    job, done = _start_persisted_job(api_session_scope, n_trials=2)
    in_memory = strategy_search_jobs.results_payload(done)
    strategy_search_jobs._jobs.clear()

    with patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope):
        rebuilt = get_strategy_search_results(job.run_id)

    assert rebuilt["run_id"] == job.run_id
    assert len(rebuilt["candidates"]) == len(in_memory["candidates"])
    assert rebuilt["best"]["candidate_id"] == in_memory["best"]["candidate_id"]


def test_strategy_search_cancel_between_candidates(api_session_scope):
    original_run = StrategySearchRunner.run

    def slow_run(self, progress_callback=None, should_stop=None):
        time.sleep(0.25)
        return original_run(self, progress_callback=progress_callback, should_stop=should_stop)

    with (
        patch("q_backend.api.strategy_search_jobs.session_scope", api_session_scope),
        patch.object(StrategySearchRunner, "run", slow_run),
    ):
        job = strategy_search_jobs.start_job(
            _request(n_trials=2),
            backtest_runner=_backtest_runner(),
            provider=_provider(),
        )
        strategy_search_jobs.request_cancel(job.run_id)
        finished = _wait_for(job.run_id, {"cancelled"})

    assert finished.status == "cancelled"
    assert finished.result is not None
    assert len(finished.result.candidates) < finished.total_candidates


def test_list_and_delete_strategy_search(
    api_db_session, api_session_scope, lake_root_path
):
    job, _done = _start_persisted_job(api_session_scope, n_trials=2)
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
            "backtest": request.backtest.model_copy(
                update={"end": datetime(2024, 2, 1)}
            ),
            "walkforward": request.walkforward.model_copy(
                update={"train_days": 30, "test_days": 30, "min_windows": 2}
            ),
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
            objective=ObjectiveConfig(
                mode=ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN
            ),
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
    upgrade_ops = revision.module.upgrade
    downgrade_ops = revision.module.downgrade
    assert callable(upgrade_ops)
    assert callable(downgrade_ops)
