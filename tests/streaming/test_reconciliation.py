from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import select

from q_backend.api import (
    alpha_research_jobs,
    backtest_jobs,
    discovery_ab_jobs,
    encoder_ablation_jobs,
    optimization_jobs,
    strategy_search_jobs,
    walkforward_jobs,
)
from q_backend.storage.db.models import (
    BacktestConfig,
    BacktestRun,
    OptimizationStudy,
    Strategy,
    StrategySearchRun,
    WalkForwardRun,
)
from q_backend.storage.db.outbox_models import JobTerminalMarker, OutboxEvent


def _get_terminal_events(session_scope, kind: str, job_id: str) -> list[OutboxEvent]:
    with session_scope() as session:
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "jobs.terminal").order_by(OutboxEvent.seq.asc())
            )
            .scalars()
            .all()
        )
        return [e for e in events if e.payload.get("kind") == kind and e.payload.get("job_id") == job_id]


def test_reconcile_orphaned_backtest(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    with session_scope() as session:
        cfg = BacktestConfig(
            name=f"cfg-{uuid.uuid4().hex[:8]}",
            config={"symbol": "EURUSD"},
        )
        session.add(cfg)
        session.flush()

        run = BacktestRun(
            backtest_config_id=cfg.id,
            status="running",
            config={"symbol": "EURUSD"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        run_id = str(run.id)

    count = backtest_jobs.reconcile_orphaned_runs()
    assert count >= 1

    with session_scope() as session:
        refreshed = session.get(BacktestRun, uuid.UUID(run_id))
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    events = _get_terminal_events(session_scope, "backtest", run_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "backtest"
    assert events[0].payload["job_id"] == run_id

    # Second call must produce no additional events
    count2 = backtest_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "backtest", run_id)
    assert len(events2) == 1


def test_reconcile_orphaned_optimization(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    with session_scope() as session:
        study = OptimizationStudy(
            name=f"study-{uuid.uuid4().hex[:8]}",
            status="running",
            config={"test": True},
        )
        session.add(study)
        session.flush()
        study_id = study.id.hex

    count = optimization_jobs.reconcile_orphaned_runs()
    assert count >= 1

    with session_scope() as session:
        refreshed = session.get(OptimizationStudy, uuid.UUID(hex=study_id))
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    events = _get_terminal_events(session_scope, "optimization", study_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "optimization"
    assert events[0].payload["job_id"] == study_id

    # Second call
    count2 = optimization_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "optimization", study_id)
    assert len(events2) == 1


def test_reconcile_orphaned_walkforward(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    with session_scope() as session:
        wf = WalkForwardRun(
            name=f"wf-{uuid.uuid4().hex[:8]}",
            status="running",
            config={"test": True},
        )
        session.add(wf)
        session.flush()
        wf_id = wf.id.hex

    count = walkforward_jobs.reconcile_orphaned_runs()
    assert count >= 1

    with session_scope() as session:
        refreshed = session.get(WalkForwardRun, uuid.UUID(hex=wf_id))
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    events = _get_terminal_events(session_scope, "walkforward", wf_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "walkforward"
    assert events[0].payload["job_id"] == wf_id

    # Second call
    count2 = walkforward_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "walkforward", wf_id)
    assert len(events2) == 1


def test_reconcile_orphaned_strategy_search(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    with session_scope() as session:
        ss = StrategySearchRun(
            name=f"ss-{uuid.uuid4().hex[:8]}",
            status="running",
            config={"test": True},
        )
        session.add(ss)
        session.flush()
        ss_id = ss.id.hex

    count = strategy_search_jobs.reconcile_orphaned_runs()
    assert count >= 1

    with session_scope() as session:
        refreshed = session.get(StrategySearchRun, uuid.UUID(hex=ss_id))
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    events = _get_terminal_events(session_scope, "strategy_search", ss_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "strategy_search"
    assert events[0].payload["job_id"] == ss_id

    # Second call
    count2 = strategy_search_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "strategy_search", ss_id)
    assert len(events2) == 1


def test_reconcile_orphaned_discovery_ab(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    job_id = f"orphan-discovery-ab-{uuid.uuid4().hex[:8]}"
    discovery_ab_jobs._persist_progress(
        job_id,
        discovery_ab_jobs._base_payload(job_id, status="running", progress=0.5),
    )

    count = discovery_ab_jobs.reconcile_orphaned_runs()
    assert count >= 1

    payload = discovery_ab_jobs.get_discovery_ab_status_payload(job_id)
    assert payload is not None
    assert payload["status"] == "failed"

    events = _get_terminal_events(session_scope, "discovery_ab", job_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "discovery_ab"
    assert events[0].payload["job_id"] == job_id

    # Second call
    count2 = discovery_ab_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "discovery_ab", job_id)
    assert len(events2) == 1


def test_reconcile_orphaned_encoder_ablation(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    job_id = f"orphan-ablation-{uuid.uuid4().hex[:8]}"
    encoder_ablation_jobs._persist_progress(
        job_id,
        encoder_ablation_jobs._base_payload(job_id, status="running", progress="1/2"),
    )

    count = encoder_ablation_jobs.reconcile_orphaned_runs()
    assert count >= 1

    payload = encoder_ablation_jobs.get_encoder_ablation_status_payload(job_id)
    assert payload is not None
    assert payload["status"] == "failed"

    events = _get_terminal_events(session_scope, "encoder_ablation", job_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "encoder_ablation"
    assert events[0].payload["job_id"] == job_id

    # Second call
    count2 = encoder_ablation_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "encoder_ablation", job_id)
    assert len(events2) == 1


def test_reconcile_orphaned_alpha_research(run_jobs_sync) -> None:
    session_scope = run_jobs_sync.harness_session_scope
    job_id = f"orphan-alpha-{uuid.uuid4().hex[:8]}"
    alpha_research_jobs._persist_progress(
        job_id,
        alpha_research_jobs._base_payload(job_id, status="running", progress=0.5),
    )

    count = alpha_research_jobs.reconcile_orphaned_runs()
    assert count >= 1

    payload = alpha_research_jobs.get_alpha_research_status_payload(job_id)
    assert payload is not None
    assert payload["status"] == "failed"

    events = _get_terminal_events(session_scope, "alpha_research", job_id)
    assert len(events) == 1
    assert events[0].payload["status"] == "cancelled"
    assert events[0].payload["kind"] == "alpha_research"
    assert events[0].payload["job_id"] == job_id

    # Second call
    count2 = alpha_research_jobs.reconcile_orphaned_runs()
    assert count2 == 0
    events2 = _get_terminal_events(session_scope, "alpha_research", job_id)
    assert len(events2) == 1
