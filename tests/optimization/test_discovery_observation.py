import uuid

import fakeredis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from q_backend.optimization.discovery_observation import (
    activity_snapshot,
    observe_candidate,
    observe_window,
)
from q_backend.storage.db.base import Base
from q_backend.storage.db.discovery_trials import list_trials, save_trial
from q_backend.storage.db.models import StrategySearchRun


def test_trial_completion_publishes_without_waiting_for_candidate(monkeypatch):
    import optuna
    from q_backend.optimization import discovery_observation as observation

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(observation, "get_redis", lambda: client)
    published = []
    monkeypatch.setattr(observation, "publish_job_progress", lambda *args, **kwargs: published.append(args))
    observation.initialize("run", 2, 2, 3)
    observe_candidate("run", "A", strategy="A", total_windows=2, client=client)
    study = optuna.create_study()
    study.optimize(lambda trial: 1.0, n_trials=1)
    observation.record_trial("run", "A", 0, study.trials[0])
    assert len(published) >= 3
    assert observation.snapshot("run")["progress"] > 0


def test_live_results_endpoint_exposes_finished_candidates_before_finalizer(monkeypatch):
    from q_backend.api import strategy_search_jobs as jobs
    from q_backend.api.routers.strategy_search import get_strategy_search_results
    from q_backend.optimization import discovery_observation as observation
    from q_backend.optimization.strategy_search import CandidateResult
    from q_backend.tasks.serialization import candidate_result_to_dict

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(observation, "get_redis", lambda: client)
    observation.remember_result(
        "run", candidate_result_to_dict(CandidateResult(candidate_id="A", strategy="A", status="completed"))
    )
    monkeypatch.setattr(
        jobs,
        "get_status_payload",
        lambda run_id: dict(status="running", search_config={"objective": {"mode": "maximize_net_profit"}}),
    )
    monkeypatch.setattr(jobs, "get_job", lambda run_id: None)
    monkeypatch.setattr(jobs, "results_payload_from_db", lambda run_id: None)
    monkeypatch.setattr(jobs, "get_persisted_run_status", lambda run_id: "running")
    results = get_strategy_search_results("run")
    assert results["status"] == "running"
    assert results["provisional"] is True
    assert results["candidates"][0]["candidate_id"] == "A"


def test_activity_is_idempotent_and_window_testing_is_not_completion():
    client = fakeredis.FakeRedis(decode_responses=True)
    observe_candidate("run", "A", strategy="A", total_windows=2, client=client)
    observe_window("run", "A", 0, phase="testing", trials=3, client=client)
    snap = activity_snapshot("run", total_candidates=2, windows_per_candidate=2, trial_budget=3, client=client)
    assert snap["finished_windows"] == 0
    assert 0 < snap["progress"] < 1
    observe_window("run", "A", 0, phase="done", trials=3, client=client)
    observe_window("run", "A", 0, phase="done", trials=3, client=client)
    observe_window("run", "A", 0, phase="optimizing", trials=1, client=client)
    snap = activity_snapshot("run", total_candidates=2, windows_per_candidate=2, trial_budget=3, client=client)
    assert snap["finished_windows"] == 1
    observe_candidate("run", "A", strategy="A", total_windows=2, state="completed", client=client)
    observe_candidate("run", "B", strategy="B", total_windows=2, state="unsupported", client=client)
    snap = activity_snapshot("run", total_candidates=2, windows_per_candidate=2, trial_budget=3, client=client)
    assert snap["completed_candidates"] == 2
    assert snap["progress"] == 1


def test_all_trials_survive_upserts_filtering_and_run_deletion():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    run_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(StrategySearchRun(id=run_id, name="discovery", status="running", config={}))
        session.flush()
        for number in range(125):
            save_trial(
                session,
                run_id,
                dict(
                    candidate_id="A",
                    generation=-1,
                    window_index=0,
                    trial_number=number,
                    state="completed",
                    objective_value=float(number),
                    parameters={"period": number},
                    metrics={},
                ),
            )
        save_trial(
            session,
            run_id,
            dict(
                candidate_id="A",
                generation=-1,
                window_index=0,
                trial_number=124,
                state="completed",
                objective_value=124.0,
                parameters={"period": 124},
                metrics={},
            ),
        )
        session.commit()
        page = list_trials(session, run_id, limit=20, offset=0, objective_min=110)
        assert page["total"] == 15
        assert len(page["items"]) == 15
        assert list_trials(session, run_id, limit=20, offset=20)["total"] == 125
        assert list_trials(session, run_id, q="period", state="pruned")["total"] == 0
        session.delete(session.get(StrategySearchRun, run_id))
        session.commit()
        assert list_trials(session, run_id)["total"] == 0


def test_trial_persistence_survives_redis_outage_and_classifies_errors(monkeypatch):
    import optuna
    import redis
    from contextlib import contextmanager
    from q_backend.optimization import discovery_observation as observation

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    run_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(StrategySearchRun(id=run_id, name="discovery", status="running", config={}))
        session.commit()

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    def unavailable():
        raise redis.ConnectionError("unavailable")

    monkeypatch.setattr(observation, "session_scope", scope)
    monkeypatch.setattr(observation, "get_redis", unavailable)
    trial = optuna.trial.create_trial(value=float("inf"), user_attrs={"status": "error", "error": "backtest failed"})
    trial.number = 7
    observation.record_trial(str(run_id), "A", 0, trial, generation=2)
    with Session(engine) as session:
        page = list_trials(session, run_id, generation=2, state="error")
        assert page["total"] == 1
        assert page["items"][0]["objective_value"] is None
        assert page["items"][0]["error"] == "backtest failed"


def test_trial_activity_counts_unique_completions_instead_of_trial_numbers(monkeypatch):
    import optuna
    from contextlib import contextmanager
    from q_backend.optimization import discovery_observation as observation

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(observation, "get_redis", lambda: client)
    monkeypatch.setattr(observation, "publish_job_progress", lambda *args, **kwargs: None)

    class EmptySession:
        def get(self, *args):
            return None

    @contextmanager
    def scope():
        yield EmptySession()

    monkeypatch.setattr(observation, "session_scope", scope)
    run_id = str(uuid.uuid4())
    observation.initialize(run_id, 1, 1, 10)
    for number in (9, 2, 9):
        trial = optuna.trial.create_trial(value=1.0)
        trial.number = number
        observation.record_trial(run_id, "A", 0, trial)
    assert observation.snapshot(run_id)["settled_trials"] == 2


def test_completed_candidate_survives_failed_finalization_and_redis_expiry(monkeypatch):
    from contextlib import contextmanager
    from q_backend.api import strategy_search_jobs as jobs
    from q_backend.optimization import discovery_observation as observation
    from q_backend.optimization.strategy_search import CandidateResult
    from q_backend.tasks.serialization import candidate_result_to_dict

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    run_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(StrategySearchRun(id=run_id, name="discovery", status="failed", config={}))
        session.commit()

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(observation, "session_scope", scope)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(observation, "get_redis", lambda: client)
    observation.remember_result(
        str(run_id), candidate_result_to_dict(CandidateResult(candidate_id="A", strategy="A", status="completed"))
    )
    observation.clear(str(run_id))
    results = jobs.results_payload_from_db(str(run_id))
    assert results["status"] == "failed"
    assert results["candidates"][0]["candidate_id"] == "A"
    assert results["artifacts_available"] is False


def test_trial_migration_upgrades_and_downgrades(monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    from q_backend.storage.db.models import StrategySearchTrial

    migration_path = Path(__file__).parents[2] / "alembic/versions/20261010_0028_discovery_trials.py"
    spec = importlib.util.spec_from_file_location("discovery_trial_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    StrategySearchTrial.__table__.drop(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        inspector = inspect(connection)
        assert "strategy_search_trials" in inspector.get_table_names()
        assert inspector.get_unique_constraints("strategy_search_trials")[0]["column_names"] == [
            "run_id",
            "candidate_id",
            "generation",
            "window_index",
            "trial_number",
        ]
        migration.downgrade()
        assert "strategy_search_trials" not in inspect(connection).get_table_names()
