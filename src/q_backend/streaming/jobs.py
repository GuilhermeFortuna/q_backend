from collections.abc import Mapping
from datetime import datetime
import logging
import threading
from typing import Any, Literal
import redis
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.outbox_models import JobTerminalMarker
from q_backend.storage.settings import get_settings
from q_backend.streaming.outbox import record_event
from q_backend.streaming.publisher import EphemeralPublisher

logger = logging.getLogger(__name__)

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

NAMESPACE_TO_KIND: Mapping[str, JobKind] = {
    "alpha_research": "alpha_research",
    "backtest": "backtest",
    "discovery_ab": "discovery_ab",
    "encoder_ablation": "encoder_ablation",
    "job": "optimization",
    "neural_training": "neural_training",
    "optimization": "optimization",
    "storage_ingest": "storage_ingest",
    "strategy_search": "strategy_search",
    "walkforward": "walkforward",
}

StreamStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

STATUS_TO_STREAM: Mapping[str, StreamStatus] = {
    "cancelled": "cancelled",
    "completed": "completed",
    "done": "completed",
    "error": "failed",
    "failed": "failed",
    "no_result": "failed",
    "pending": "queued",
    "queued": "queued",
    "running": "running",
}

_publisher_client: redis.Redis | None = None
PUBLISH_TIMEOUT_S = 0.05
_BOUNDED_KWARGS = {
    "socket_timeout": PUBLISH_TIMEOUT_S,
    "socket_connect_timeout": PUBLISH_TIMEOUT_S,
    "retry_on_timeout": False,
    "retry": None,
}
_bounded_clients: dict[tuple[Any, ...], redis.Redis] = {}
_bounded_clients_lock = threading.Lock()


def _bounded_client(client: Any) -> Any:
    """A client for the same server with publish timeouts, leaving `client` untouched.

    The caller's client is shared with its other Redis work, whose timeouts must not
    change. Bounded clients are cached by connection settings so repeated progress
    updates reuse one pool. Clients without a network pool (fakeredis) are used as is.
    """
    pool = getattr(client, "connection_pool", None)
    connection_class = getattr(pool, "connection_class", None)
    if not isinstance(pool, redis.ConnectionPool) or not (
        isinstance(connection_class, type) and issubclass(connection_class, redis.Connection)
    ):
        return client
    kwargs = {**pool.connection_kwargs, **_BOUNDED_KWARGS}
    key = (connection_class, tuple(sorted((name, repr(value)) for name, value in kwargs.items())))
    with _bounded_clients_lock:
        bounded = _bounded_clients.get(key)
        if bounded is None:
            bounded = redis.Redis(connection_pool=redis.ConnectionPool(connection_class=connection_class, **kwargs))
            _bounded_clients[key] = bounded
    return bounded


def get_publisher_client() -> redis.Redis:
    if _publisher_client is not None:
        return _bounded_client(_publisher_client)
    return _bounded_client(redis.Redis.from_url(get_settings().redis_url, decode_responses=True))


def set_publisher_client(client: redis.Redis | None) -> None:
    global _publisher_client
    _publisher_client = client


def _terminal_flag_key(kind: str, job_id: str) -> str:
    return f"stream:job:terminal:{kind}:{job_id}"


def is_job_terminal_flagged(client: redis.Redis, kind: str, job_id: str) -> bool:
    try:
        return bool(client.exists(_terminal_flag_key(kind, job_id)))
    except Exception as exc:  # noqa: BLE001 - best-effort Redis terminal check
        logger.debug("Failed to check terminal flag in Redis for %s:%s: %s", kind, job_id, exc)
        return False


def flag_job_terminal(client: redis.Redis, kind: str, job_id: str, ttl_seconds: int = 86400) -> None:
    try:
        client.set(_terminal_flag_key(kind, job_id), "1", ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - best-effort Redis terminal flag
        logger.debug("Failed to set terminal flag in Redis for %s:%s: %s", kind, job_id, exc)


def clear_job_terminal_flag(client: redis.Redis, kind: str, job_id: str) -> None:
    try:
        client.delete(_terminal_flag_key(kind, job_id))
    except Exception as exc:  # noqa: BLE001 - best-effort Redis terminal clear
        logger.debug("Failed to clear terminal flag in Redis for %s:%s: %s", kind, job_id, exc)


def publish_job_progress(
    kind: JobKind,
    job_id: str,
    raw_payload: Mapping[str, Any],
    *,
    client: redis.Redis | None = None,
) -> None:
    """Map and publish to jobs.progress; bounded, never raises; skipped once terminal."""
    try:
        if _publisher_client is None and client is not None:
            redis_client = _bounded_client(client)
        else:
            redis_client = get_publisher_client()

        # If job is already flagged terminal, do not publish
        if is_job_terminal_flagged(redis_client, kind, job_id):
            return

        # Map status
        raw_status = str(raw_payload.get("status", "running")).lower()
        if raw_status not in STATUS_TO_STREAM:
            return
        stream_status = STATUS_TO_STREAM[raw_status]

        # Only non-terminal statuses ("queued", "running") travel on jobs.progress
        if stream_status not in ("queued", "running"):
            return

        # Map progress and message
        raw_progress = raw_payload.get("progress")
        message = raw_payload.get("message")

        if isinstance(raw_progress, str):
            progress = None
            if not message:
                message = raw_progress
        elif isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
            progress = max(0.0, min(1.0, float(raw_progress)))
        else:
            progress = None

        if not message and "detail" in raw_payload and raw_payload["detail"]:
            message = str(raw_payload["detail"])

        payload: dict[str, Any] = {
            "kind": kind,
            "job_id": job_id,
            "status": stream_status,
            "progress": progress,
        }
        if message is not None:
            payload["message"] = str(message)

        publisher = EphemeralPublisher(redis_client, "jobs.progress", producer_id=f"job-{kind}-{job_id}")
        publisher.publish(
            routing_key={"kind": kind, "job_id": job_id},
            payload_kind="control",
            payload_schema="schema/stream/payloads/job-progress.schema.json",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - progress is observational; never raises
        logger.debug("Progress publish failed for %s:%s: %s", kind, job_id, exc)


def _ensure_session_hooks(session: Session) -> None:
    if "_job_stream_hooks_registered" in session.info:
        return
    session.info["_job_stream_hooks_registered"] = True
    session.info["_terminal_flags_to_set"] = set()
    session.info["_terminal_flags_to_clear"] = set()

    @sa_event.listens_for(session, "after_commit")
    def _on_commit(s: Session) -> None:
        to_set = s.info.pop("_terminal_flags_to_set", set())
        to_clear = s.info.pop("_terminal_flags_to_clear", set())
        s.info["_terminal_flags_to_set"] = set()
        s.info["_terminal_flags_to_clear"] = set()
        try:
            client = get_publisher_client()
            for k, jid in to_clear:
                clear_job_terminal_flag(client, k, jid)
            for k, jid in to_set:
                flag_job_terminal(client, k, jid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed setting terminal flags after commit: %s", exc)

    @sa_event.listens_for(session, "after_rollback")
    def _on_rollback(s: Session) -> None:
        s.info.pop("_terminal_flags_to_set", set())
        s.info.pop("_terminal_flags_to_clear", set())
        s.info["_terminal_flags_to_set"] = set()
        s.info["_terminal_flags_to_clear"] = set()


def _queue_terminal_flag_set(session: Session, kind: str, job_id: str) -> None:
    _ensure_session_hooks(session)
    session.info.setdefault("_terminal_flags_to_set", set()).add((kind, job_id))
    if "_terminal_flags_to_clear" in session.info:
        session.info["_terminal_flags_to_clear"].discard((kind, job_id))


def _queue_terminal_flag_clear(session: Session, kind: str, job_id: str) -> None:
    _ensure_session_hooks(session)
    session.info.setdefault("_terminal_flags_to_clear", set()).add((kind, job_id))
    if "_terminal_flags_to_set" in session.info:
        session.info["_terminal_flags_to_set"].discard((kind, job_id))


def record_job_terminal(
    session: Session,
    kind: JobKind,
    job_id: str,
    raw_status: str,
    *,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> bool:
    """Record a terminal job outcome to the stream outbox exactly once.

    Returns True if the terminal event was recorded, or False if this job
    was already marked terminal. Raises ValueError if raw_status is not terminal.
    """
    raw_status_lower = str(raw_status).lower()
    if raw_status_lower not in STATUS_TO_STREAM:
        raise ValueError(f"Status {raw_status!r} is not a terminal status")

    stream_status = STATUS_TO_STREAM[raw_status_lower]
    if stream_status not in ("completed", "failed", "cancelled"):
        raise ValueError(f"Status {raw_status!r} is not a terminal status")

    event_ts = finished_at or utc_now()

    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name == "postgresql":
        stmt = (
            pg_insert(JobTerminalMarker)
            .values(
                kind=kind,
                job_id=job_id,
                status=stream_status,
                outbox_seq=0,
                recorded_at=event_ts,
            )
            .on_conflict_do_nothing()
            .returning(JobTerminalMarker.kind)
        )
        res = session.execute(stmt).first()
        if res is None:
            return False
    elif dialect_name == "sqlite":
        stmt = (
            sqlite_insert(JobTerminalMarker)
            .values(
                kind=kind,
                job_id=job_id,
                status=stream_status,
                outbox_seq=0,
                recorded_at=event_ts,
            )
            .on_conflict_do_nothing()
            .returning(JobTerminalMarker.kind)
        )
        res = session.execute(stmt).first()
        if res is None:
            return False
    else:
        existing = session.scalar(
            sa.select(JobTerminalMarker).where(
                JobTerminalMarker.kind == kind,
                JobTerminalMarker.job_id == job_id,
            )
        )
        if existing is not None:
            return False
        marker = JobTerminalMarker(
            kind=kind,
            job_id=job_id,
            status=stream_status,
            outbox_seq=0,
            recorded_at=event_ts,
        )
        session.add(marker)
        try:
            session.flush()
        except Exception:  # noqa: BLE001
            return False

    payload: dict[str, Any] = {
        "kind": kind,
        "job_id": job_id,
        "status": stream_status,
        "finished_at": event_ts.isoformat(),
    }
    if error is not None:
        payload["error"] = str(error)

    event = record_event(
        session,
        topic="jobs.terminal",
        payload=payload,
        payload_schema="schema/stream/payloads/job-terminal.schema.json",
        producer_id=f"job-{kind}-{job_id}",
        routing_key={"kind": kind, "job_id": job_id},
        origin_ts=event_ts,
    )

    session.execute(
        sa.update(JobTerminalMarker)
        .where(JobTerminalMarker.kind == kind, JobTerminalMarker.job_id == job_id)
        .values(outbox_seq=event.seq)
    )
    session.flush()

    _queue_terminal_flag_set(session, kind, job_id)
    return True


def clear_job_terminal_marker(session: Session, kind: str, job_id: str) -> None:
    """Clear JobTerminalMarker and Redis terminal flag on backtest rerun."""
    session.execute(
        sa.delete(JobTerminalMarker).where(
            JobTerminalMarker.kind == kind,
            JobTerminalMarker.job_id == job_id,
        )
    )
    session.flush()
    _queue_terminal_flag_clear(session, kind, job_id)
