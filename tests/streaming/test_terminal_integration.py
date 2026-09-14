import concurrent.futures
import uuid
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.models import (
    BacktestConfig,
    BacktestRun,
    OptimizationStudy,
    StrategySearchRun,
    WalkForwardRun,
)
from q_backend.storage.db.outbox_models import JobTerminalMarker, OutboxEvent
from q_backend.storage.db.repositories import (
    update_backtest_run,
    update_optimization_study,
    update_strategy_search_run,
    update_walkforward_run,
)
from q_backend.streaming.jobs import record_job_terminal


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_concurrent_record_job_terminal_exactly_one_event():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    test_job_id = f"bt-conc-{uuid.uuid4().hex[:8]}"

    # Clean any prior state for this test job
    with session_factory() as session:
        session.execute(
            text("DELETE FROM stream_job_terminal_markers WHERE kind = 'backtest' AND job_id = :jid"),
            {"jid": test_job_id},
        )
        session.execute(
            text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal' AND routing_key->>'job_id' = :jid"),
            {"jid": test_job_id},
        )
        session.commit()

    def worker(worker_id: int) -> bool:
        with session_factory() as session:
            recorded = record_job_terminal(
                session,
                kind="backtest",
                job_id=test_job_id,
                raw_status="completed",
                finished_at=None,
            )
            session.commit()
            return recorded

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, 1)
        f2 = executor.submit(worker, 2)
        results = [f1.result(), f2.result()]

    # Exactly one worker should have returned True, and the other False
    assert results.count(True) == 1
    assert results.count(False) == 1

    with session_factory() as session:
        markers = session.scalars(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "backtest",
                JobTerminalMarker.job_id == test_job_id,
            )
        ).all()
        assert len(markers) == 1

        events = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.topic == "jobs.terminal",
                OutboxEvent.producer_id == f"job-backtest-{test_job_id}",
            )
        ).all()
        assert len(events) == 1


@pytest.mark.integration
def test_update_backtest_run_rollback_leaves_no_marker_or_event():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        cfg = BacktestConfig(name=f"test-cfg-{uuid.uuid4().hex[:8]}", config={"symbol": "TEST"})
        session.add(cfg)
        session.flush()
        run = BacktestRun(backtest_config_id=cfg.id, config={"symbol": "TEST"}, status="running")
        session.add(run)
        session.commit()
        run_id = run.id

    # Update to completed inside a session that then raises and rolls back
    with pytest.raises(RuntimeError, match="simulated failure"):
        with session_factory() as session:
            update_backtest_run(session, run_id, status="completed")
            raise RuntimeError("simulated failure")

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "backtest",
                JobTerminalMarker.job_id == str(run_id),
            )
        )
        assert marker is None

        events = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.topic == "jobs.terminal",
                OutboxEvent.producer_id == f"job-backtest-{run_id}",
            )
        ).all()
        assert len(events) == 0


@pytest.mark.integration
def test_repository_terminal_transitions_and_rerun_clearing():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    # 1. BacktestRun: transition to completed, then rerun back to running
    with session_factory() as session:
        cfg = BacktestConfig(name=f"test-cfg-{uuid.uuid4().hex[:8]}", config={"symbol": "TEST"})
        session.add(cfg)
        session.flush()
        run = BacktestRun(backtest_config_id=cfg.id, config={"symbol": "TEST"}, status="running")
        session.add(run)
        session.commit()
        bt_id = run.id

    with session_factory() as session:
        update_backtest_run(session, bt_id, status="completed")
        session.commit()

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "backtest",
                JobTerminalMarker.job_id == str(bt_id),
            )
        )
        assert marker is not None
        assert marker.status == "completed"

    # Rerun back to running clears marker
    with session_factory() as session:
        update_backtest_run(session, bt_id, status="running")
        session.commit()

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "backtest",
                JobTerminalMarker.job_id == str(bt_id),
            )
        )
        assert marker is None

    # 2. OptimizationStudy: transition to completed
    with session_factory() as session:
        study = OptimizationStudy(name=f"study-{uuid.uuid4().hex[:8]}", config={}, status="running")
        session.add(study)
        session.commit()
        study_id = study.id

    with session_factory() as session:
        update_optimization_study(session, study_id, status="done")
        session.commit()

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "optimization",
                JobTerminalMarker.job_id == study_id.hex,
            )
        )
        assert marker is not None
        assert marker.status == "completed"

    # 3. WalkForwardRun: transition to completed
    with session_factory() as session:
        wf_run = WalkForwardRun(
            name=f"wf-{uuid.uuid4().hex[:8]}",
            config={"optimization": {}, "walkforward": {}},
            status="running",
        )
        session.add(wf_run)
        session.commit()
        wf_id = wf_run.id

    with session_factory() as session:
        update_walkforward_run(session, wf_id, status="completed")
        session.commit()

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "walkforward",
                JobTerminalMarker.job_id == str(wf_id),
            )
        )
        assert marker is not None
        assert marker.status == "completed"

    # 4. StrategySearchRun: transition to failed with error
    with session_factory() as session:
        ss_run = StrategySearchRun(
            name=f"ss-{uuid.uuid4().hex[:8]}",
            config={},
            status="running",
        )
        session.add(ss_run)
        session.commit()
        ss_id = ss_run.id

    with session_factory() as session:
        update_strategy_search_run(session, ss_id, status="failed", error_message="Search exhausted")
        session.commit()

    with session_factory() as session:
        marker = session.scalar(
            select(JobTerminalMarker).where(
                JobTerminalMarker.kind == "strategy_search",
                JobTerminalMarker.job_id == str(ss_id),
            )
        )
        assert marker is not None
        assert marker.status == "failed"
