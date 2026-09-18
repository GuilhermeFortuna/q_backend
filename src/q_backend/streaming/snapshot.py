"""Stream replay helpers: history, latest values, and job snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import redis
import sqlalchemy as sa
from redis.exceptions import RedisError
from sqlalchemy.orm import Session, sessionmaker

from q_backend.execution.domain import DeploymentLifecycle, ExecutionOrderStatus
from q_backend.storage.db.base import utc_now
from q_backend.storage.db.execution_models import (
    ExecutionControlState,
    ExecutionDecision,
    ExecutionDeployment,
    ExecutionFill,
    ExecutionNetPosition,
    ExecutionOrder,
    ExecutionRiskEvent,
    PaperAccount,
)
from q_backend.storage.db.models import BacktestRun, OptimizationStudy, StrategySearchRun, WalkForwardRun
from q_backend.storage.db.outbox_models import INITIAL_EPOCH, JobTerminalMarker, OutboxEvent
from q_backend.storage.redis.progress import get_job_progress
from q_backend.streaming import execution_events
from q_backend.streaming.codec import decode_entry, envelope_to_dict, outbox_event_to_envelope, routing_key_string
from q_backend.streaming.jobs import STATUS_TO_STREAM, StreamStatus
from q_backend.streaming.keys import latest_key, stream_key
from q_backend.streaming.outbox import OutboxTopicError, oldest_retained_seq, read_watermark
from q_contracts.stream import StreamEnvelope
from q_contracts.topics import TOPICS

JobKind = Literal[
    "alpha_research",
    "backtest",
    "discovery_ab",
    "encoder_ablation",
    "neural_training",
    "optimization",
    "storage_ingest",
    "strategy_search",
    "walkforward",
]

TABLE_KINDS: dict[str, tuple[type, Any]] = {
    "backtest": (BacktestRun, lambda run: str(run.id)),
    "optimization": (OptimizationStudy, lambda run: str(run.id)),
    "walkforward": (WalkForwardRun, lambda run: str(run.id)),
    "strategy_search": (StrategySearchRun, lambda run: str(run.id)),
}

REDIS_ONLY_KINDS: tuple[JobKind, ...] = (
    "alpha_research",
    "discovery_ab",
    "encoder_ablation",
    "neural_training",
    "storage_ingest",
)

KIND_TO_NAMESPACE: dict[str, str] = {
    "backtest": "backtest",
    "optimization": "job",
    "walkforward": "walkforward",
    "strategy_search": "strategy_search",
    "alpha_research": "alpha_research",
    "discovery_ab": "discovery_ab",
    "encoder_ablation": "encoder_ablation",
    "neural_training": "neural_training",
    "storage_ingest": "storage_ingest",
}


class HistoryExpired(Exception):
    def __init__(self, topic: str, requested_from_seq: int, oldest_available_seq: int | None) -> None:
        self.topic = topic
        self.requested_from_seq = requested_from_seq
        self.oldest_available_seq = oldest_available_seq


class EpochMismatch(Exception):
    def __init__(self, topic: str, requested_epoch: str, current_epoch: str) -> None:
        self.topic = topic
        self.requested_epoch = requested_epoch
        self.current_epoch = current_epoch


class StreamUnavailable(Exception):
    """Raised when Redis is unavailable for ephemeral replay."""


def _text_key(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


@dataclass(frozen=True)
class HistoryResult:
    topic: str
    epoch: str
    entries: list[StreamEnvelope]
    next_seq: int | None


@dataclass(frozen=True)
class JobSnapshotItem:
    kind: str
    job_id: str
    status: StreamStatus
    progress: float | None
    message: str | None
    progress_seq: int | None
    progress_epoch: str | None


@dataclass(frozen=True)
class JobSnapshotResult:
    jobs: list[JobSnapshotItem]
    watermark: dict[str, dict[str, Any]]


def read_history(session: Session, topic: str, epoch: str, from_seq: int, limit: int) -> HistoryResult:
    """Read a page of durable topic history from the outbox."""
    if topic not in TOPICS:
        raise ValueError(f"Unknown topic: {topic!r}")
    if TOPICS[topic].topic_class != "durable":
        raise OutboxTopicError(f"Topic {topic!r} is not a declared durable topic")

    watermark = read_watermark(session, [topic])
    current_epoch, _current_seq = watermark[topic]
    if epoch != current_epoch:
        raise EpochMismatch(topic, epoch, current_epoch)

    oldest = oldest_retained_seq(session, topic)
    if oldest is not None and from_seq < oldest:
        raise HistoryExpired(topic, from_seq, oldest)

    rows = (
        session.execute(
            sa.select(OutboxEvent)
            .where(
                OutboxEvent.topic == topic,
                OutboxEvent.epoch == epoch,
                OutboxEvent.seq >= from_seq,
            )
            .order_by(OutboxEvent.seq)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )

    if len(rows) > limit:
        next_seq = rows[limit].seq
        page_rows = rows[:limit]
    else:
        next_seq = None
        page_rows = rows

    entries = [outbox_event_to_envelope(row) for row in page_rows]
    return HistoryResult(topic=topic, epoch=epoch, entries=entries, next_seq=next_seq)


def read_latest(client: redis.Redis, topic: str, key: str | None = None) -> dict[str, StreamEnvelope]:
    """Read latest envelopes for an ephemeral topic from Redis."""
    if topic not in TOPICS:
        raise ValueError(f"Unknown topic: {topic!r}")
    if TOPICS[topic].topic_class != "ephemeral":
        raise OutboxTopicError(f"Topic {topic!r} is not a declared ephemeral topic")

    try:
        client.ping()
    except RedisError as exc:
        raise StreamUnavailable from exc

    latest_hash = latest_key(topic)
    if key is not None:
        stream_id = client.hget(latest_hash, key)
        routing_entries = {key: stream_id} if stream_id is not None else {}
    else:
        raw_entries = client.hgetall(latest_hash)
        routing_entries = {_text_key(rk): stream_id for rk, stream_id in raw_entries.items()}

    result: dict[str, StreamEnvelope] = {}
    sk = stream_key(topic)
    for routing_key_str, stream_id in routing_entries.items():
        if not stream_id:
            continue
        routing_key_str = _text_key(routing_key_str)
        if isinstance(stream_id, bytes):
            stream_id = stream_id.decode("utf-8")
        raw_entries = client.xrange(sk, min=stream_id, max=stream_id, count=1)
        if not raw_entries:
            continue
        envelope, _payload_bytes = decode_entry(raw_entries[0][1])
        result[routing_key_str] = envelope
    return result


def _map_progress_status(raw_status: str) -> StreamStatus:
    mapped = STATUS_TO_STREAM.get(str(raw_status).lower(), "running")
    if mapped in ("completed", "failed", "cancelled"):
        return "running"
    return mapped


def _progress_fields(client: redis.Redis, kind: str, job_id: str) -> tuple[float | None, str | None]:
    namespace = KIND_TO_NAMESPACE[kind]
    payload = get_job_progress(client, job_id, namespace=namespace)
    if payload is None:
        return None, None

    raw_progress = payload.get("progress")
    message = payload.get("message")
    if isinstance(raw_progress, str):
        progress = None
        if not message:
            message = raw_progress
    elif isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
        progress = max(0.0, min(1.0, float(raw_progress)))
    else:
        progress = None

    if not message and payload.get("detail"):
        message = str(payload["detail"])
    return progress, message


def _latest_progress_ref(client: redis.Redis, kind: str, job_id: str) -> tuple[int | None, str | None]:
    routing_key = {"kind": kind, "job_id": job_id}
    rk_str = routing_key_string("jobs.progress", routing_key)
    stream_id = client.hget(latest_key("jobs.progress"), rk_str)
    if stream_id is None:
        return None, None

    raw_entries = client.xrange(stream_key("jobs.progress"), min=stream_id, max=stream_id, count=1)
    if not raw_entries:
        return None, None
    envelope, _payload_bytes = decode_entry(raw_entries[0][1])
    return envelope.seq, envelope.epoch


def _scan_redis_only_active_jobs(client: redis.Redis) -> set[tuple[str, str]]:
    active: set[tuple[str, str]] = set()
    for kind in REDIS_ONLY_KINDS:
        namespace = KIND_TO_NAMESPACE[kind]
        for redis_key in client.scan_iter(match=f"{namespace}:progress:*", count=500):
            job_id = str(redis_key).rsplit(":", 1)[-1]
            active.add((kind, job_id))
    return active


def _read_postgres_snapshot(
    session: Session, *, terminal_window: timedelta
) -> tuple[dict[str, tuple[str, int]], dict[tuple[str, str], JobTerminalMarker], set[tuple[str, str]]]:
    watermark_raw = read_watermark(session, ["jobs.terminal"])
    terminal_epoch, terminal_seq = watermark_raw["jobs.terminal"]

    cutoff = utc_now() - terminal_window
    markers = session.scalars(sa.select(JobTerminalMarker).where(JobTerminalMarker.recorded_at >= cutoff)).all()
    marker_by_job = {(marker.kind, marker.job_id): marker for marker in markers}

    active_jobs: set[tuple[str, str]] = set()
    for kind, (model, id_fn) in TABLE_KINDS.items():
        runs = session.scalars(sa.select(model).where(model.status.in_(("pending", "running")))).all()
        for run in runs:
            active_jobs.add((kind, id_fn(run)))

    return (
        {"jobs.terminal": {"epoch": terminal_epoch, "seq": terminal_seq}},
        marker_by_job,
        active_jobs,
    )


def _snapshot_isolation_level(bind: Any) -> str:
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        return "REPEATABLE READ"
    return "SERIALIZABLE"


def read_job_snapshot(
    session_factory: sessionmaker[Session],
    client: redis.Redis,
    *,
    terminal_window: timedelta = timedelta(hours=24),
) -> JobSnapshotResult:
    """Build a race-free job snapshot with a terminal watermark."""
    bind = session_factory.kw["bind"]
    conn = bind.connect().execution_options(isolation_level=_snapshot_isolation_level(bind))
    trans = conn.begin()
    session = Session(bind=conn, expire_on_commit=False)
    try:
        watermark, marker_by_job, active_jobs = _read_postgres_snapshot(session, terminal_window=terminal_window)
        trans.commit()
    finally:
        session.close()
        conn.close()

    active_jobs |= _scan_redis_only_active_jobs(client)

    all_job_keys: set[tuple[str, str]] = set(active_jobs)
    all_job_keys.update(marker_by_job.keys())

    jobs: list[JobSnapshotItem] = []
    for kind, job_id in sorted(all_job_keys):
        marker = marker_by_job.get((kind, job_id))
        if marker is not None:
            status: StreamStatus = marker.status  # type: ignore[assignment]
        else:
            namespace = KIND_TO_NAMESPACE[kind]
            payload = get_job_progress(client, job_id, namespace=namespace)
            raw_status = payload.get("status", "running") if payload else "running"
            status = _map_progress_status(str(raw_status))

        progress, message = _progress_fields(client, kind, job_id)
        progress_seq, progress_epoch = _latest_progress_ref(client, kind, job_id)
        jobs.append(
            JobSnapshotItem(
                kind=kind,
                job_id=job_id,
                status=status,
                progress=progress,
                message=message,
                progress_seq=progress_seq,
                progress_epoch=progress_epoch,
            )
        )

    return JobSnapshotResult(jobs=jobs, watermark=watermark)


def job_snapshot_to_response(result: JobSnapshotResult) -> dict[str, Any]:
    """Serialize a job snapshot for JSON responses."""
    return {
        "jobs": [
            {
                "kind": item.kind,
                "job_id": item.job_id,
                "status": item.status,
                "progress": item.progress,
                "message": item.message,
                "progress_seq": item.progress_seq,
                "progress_epoch": item.progress_epoch,
            }
            for item in result.jobs
        ],
        "watermark": result.watermark,
    }


def latest_to_response(topic: str, entries: dict[str, StreamEnvelope]) -> dict[str, Any]:
    return {
        "topic": topic,
        "entries": {key: envelope_to_dict(envelope) for key, envelope in entries.items()},
    }


def history_to_response(result: HistoryResult) -> dict[str, Any]:
    return {
        "topic": result.topic,
        "epoch": result.epoch,
        "entries": [envelope_to_dict(entry) for entry in result.entries],
        "next_seq": result.next_seq,
    }


EXECUTION_TOPICS: tuple[str, ...] = ("decisions", "orders", "fills", "risk", "ledger", "deployments")


def _read_execution_snapshot_session(
    session: Session,
    *,
    deployments_limit: int = 50,
    decisions_limit: int = 500,
    orders_limit: int = 500,
    fills_limit: int = 500,
    risk_limit: int = 500,
) -> dict[str, Any]:
    watermark_raw = read_watermark(session, EXECUTION_TOPICS)
    watermark = {}
    for topic in EXECUTION_TOPICS:
        if topic in watermark_raw:
            epoch, seq = watermark_raw[topic]
        else:
            epoch, seq = INITIAL_EPOCH, 0
        watermark[topic] = {"epoch": str(epoch), "seq": int(seq)}

    control = session.get(ExecutionControlState, 1)
    if control is not None:
        control_payload = execution_events.control_state(control)
    else:
        control_payload = {
            "kill_switch_enabled": False,
            "kill_switch_reason": None,
            "updated_by": None,
            "updated_at": utc_now().isoformat(),
        }

    deployments_stmt = (
        sa.select(ExecutionDeployment)
        .order_by(
            sa.case(
                {
                    DeploymentLifecycle.RUNNING.value: 0,
                    DeploymentLifecycle.PAUSED.value: 1,
                    DeploymentLifecycle.DRAFT.value: 2,
                    DeploymentLifecycle.STOPPED.value: 3,
                    DeploymentLifecycle.ERROR.value: 4,
                },
                value=ExecutionDeployment.lifecycle,
                else_=5,
            ),
            ExecutionDeployment.created_at.desc(),
        )
        .limit(deployments_limit)
    )
    deployments = list(session.execute(deployments_stmt).scalars().all())

    accounts_stmt = sa.select(PaperAccount).order_by(PaperAccount.created_at.desc())
    accounts = list(session.execute(accounts_stmt).scalars().all())

    positions_stmt = (
        sa.select(ExecutionNetPosition)
        .where(ExecutionNetPosition.is_open.is_(True))
        .order_by(ExecutionNetPosition.opened_at.desc())
    )
    positions = list(session.execute(positions_stmt).scalars().all())

    orders_stmt = (
        sa.select(ExecutionOrder)
        .order_by(
            sa.case(
                {
                    ExecutionOrderStatus.UNKNOWN.value: 0,
                    ExecutionOrderStatus.SUBMITTED.value: 1,
                    ExecutionOrderStatus.INTENT.value: 2,
                },
                value=ExecutionOrder.status,
                else_=3,
            ),
            ExecutionOrder.created_at.desc(),
        )
        .limit(orders_limit)
    )
    orders = list(session.execute(orders_stmt).scalars().all())

    decisions_stmt = (
        sa.select(ExecutionDecision).order_by(ExecutionDecision.bar_close_time.desc()).limit(decisions_limit)
    )
    decisions = list(session.execute(decisions_stmt).scalars().all())

    fills_stmt = sa.select(ExecutionFill).order_by(ExecutionFill.filled_at.desc()).limit(fills_limit)
    fills = list(session.execute(fills_stmt).scalars().all())

    risk_stmt = sa.select(ExecutionRiskEvent).order_by(ExecutionRiskEvent.created_at.desc()).limit(risk_limit)
    risk_events = list(session.execute(risk_stmt).scalars().all())

    return {
        "deployments": [execution_events.deployment_state(d) for d in deployments],
        "accounts": [execution_events.account_state(a) for a in accounts],
        "positions": [execution_events.position_state(p) for p in positions],
        "orders": [execution_events.order_state(o) for o in orders],
        "recent": {
            "decisions": [execution_events.decision_state(d) for d in decisions],
            "fills": [execution_events.fill_event(f, None) for f in fills],
            "risk": [execution_events.risk_rejection_event(r) for r in risk_events],
        },
        "control": control_payload,
        "limits": {
            "recent_decisions": decisions_limit,
            "recent_fills": fills_limit,
            "recent_orders": orders_limit,
            "recent_risk": risk_limit,
        },
        "watermark": watermark,
    }


def read_execution_snapshot(
    session_factory: sessionmaker[Session] | None = None,
    *,
    session: Session | None = None,
    deployments_limit: int = 50,
    decisions_limit: int = 500,
    orders_limit: int = 500,
    fills_limit: int = 500,
    risk_limit: int = 500,
    ledger_limit: int = 500,
) -> dict[str, Any]:
    """Build a consistent execution state snapshot bounded by declared limits."""
    if session is not None:
        return _read_execution_snapshot_session(
            session,
            deployments_limit=deployments_limit,
            decisions_limit=decisions_limit,
            orders_limit=orders_limit,
            fills_limit=fills_limit,
            risk_limit=risk_limit,
        )

    if session_factory is None:
        raise ValueError("Either session_factory or session must be provided")

    bind = session_factory.kw["bind"]
    conn = bind.connect().execution_options(isolation_level=_snapshot_isolation_level(bind))
    trans = conn.begin()
    sess = Session(bind=conn, expire_on_commit=False)
    try:
        result = _read_execution_snapshot_session(
            sess,
            deployments_limit=deployments_limit,
            decisions_limit=decisions_limit,
            orders_limit=orders_limit,
            fills_limit=fills_limit,
            risk_limit=risk_limit,
        )
        trans.commit()
        return result
    finally:
        sess.close()
        conn.close()
