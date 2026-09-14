from collections.abc import Mapping
import logging
from typing import Any, Literal
import redis

from q_backend.storage.settings import get_settings
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


def get_publisher_client() -> redis.Redis:
    global _publisher_client
    if _publisher_client is not None:
        return _publisher_client
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=0.05,
        socket_connect_timeout=0.05,
        retry_on_timeout=False,
    )


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
        if _publisher_client is not None:
            redis_client = _publisher_client
        elif client is not None:
            redis_client = client
        else:
            redis_client = get_publisher_client()

        # Enforce 50 ms timeout bound on real Redis connection kwargs if present
        if hasattr(redis_client, "connection_pool") and hasattr(redis_client.connection_pool, "connection_kwargs"):
            kwargs = redis_client.connection_pool.connection_kwargs
            if kwargs.get("socket_timeout") is None or kwargs.get("socket_timeout") > 0.05:
                kwargs["socket_timeout"] = 0.05
            if kwargs.get("socket_connect_timeout") is None or kwargs.get("socket_connect_timeout") > 0.05:
                kwargs["socket_connect_timeout"] = 0.05
            kwargs["retry_on_timeout"] = False

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
