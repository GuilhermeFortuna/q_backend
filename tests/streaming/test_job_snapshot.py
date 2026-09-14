import uuid

import pytest
from sqlalchemy import select

from q_backend.storage.db.models import BacktestConfig, BacktestRun
from q_backend.storage.db.outbox_models import JobTerminalMarker
from q_backend.storage.redis.progress import set_job_progress
from q_backend.streaming.jobs import record_job_terminal
from q_backend.streaming.snapshot import read_job_snapshot
from tests.streaming.replay_schema import assert_valid_replay


def _create_running_backtest(session_factory) -> str:
    with session_factory() as session:
        cfg = BacktestConfig(name=f"snap-cfg-{uuid.uuid4().hex[:8]}", config={"symbol": "TEST"})
        session.add(cfg)
        session.flush()
        run = BacktestRun(backtest_config_id=cfg.id, config={"symbol": "TEST"}, status="running")
        session.add(run)
        session.commit()
        return str(run.id)


def _create_completed_backtest_with_marker(session_factory) -> str:
    with session_factory() as session:
        cfg = BacktestConfig(name=f"snap-done-{uuid.uuid4().hex[:8]}", config={"symbol": "TEST"})
        session.add(cfg)
        session.flush()
        run = BacktestRun(backtest_config_id=cfg.id, config={"symbol": "TEST"}, status="completed")
        session.add(run)
        session.flush()
        job_id = str(run.id)
        record_job_terminal(session, "backtest", job_id, "completed")
        session.commit()
        return job_id


def test_read_job_snapshot_lists_active_terminal_and_redis_only(run_jobs_sync):
    redis_client = run_jobs_sync
    session_factory = run_jobs_sync.harness_session_factory

    running_id = _create_running_backtest(session_factory)
    completed_id = _create_completed_backtest_with_marker(session_factory)

    discovery_id = f"disc-{uuid.uuid4().hex[:8]}"
    set_job_progress(
        redis_client,
        discovery_id,
        {"status": "running", "progress": 0.4, "message": "pair 2/5"},
        namespace="discovery_ab",
    )

    snapshot = read_job_snapshot(session_factory, redis_client)
    by_id = {(item.kind, item.job_id): item for item in snapshot.jobs}

    assert by_id[("backtest", running_id)].status == "running"
    assert by_id[("backtest", completed_id)].status == "completed"
    assert by_id[("discovery_ab", discovery_id)].status == "running"
    assert snapshot.watermark["jobs.terminal"]["seq"] >= 1

    assert_valid_replay("watermark", snapshot.watermark)


def test_marker_overrides_stale_redis_progress(run_jobs_sync):
    redis_client = run_jobs_sync
    session_factory = run_jobs_sync.harness_session_factory

    discovery_id = f"disc-{uuid.uuid4().hex[:8]}"
    set_job_progress(
        redis_client,
        discovery_id,
        {"status": "running", "progress": 0.9, "message": "still running in redis"},
        namespace="discovery_ab",
    )

    with session_factory() as session:
        record_job_terminal(session, "discovery_ab", discovery_id, "completed")
        session.commit()

    snapshot = read_job_snapshot(session_factory, redis_client)
    item = next(job for job in snapshot.jobs if job.job_id == discovery_id)
    assert item.status == "completed"
