"""Fan-in coordination and cancellation over Redis.

When a job fans out into N leaf messages (windows, candidates, trial batches), the
leaf that processes the final unit must run the job's finalizer. We track this with
an atomic Redis counter: each leaf decrements it, and the one that drives it to zero
is "last" and owns finalization. No Dramatiq result backend is required.

Cancellation is a simple Redis flag set by the API; leaf actors check it and skip
their work, so a cancelled job's pending messages drain quickly as no-ops.
"""

from typing import Optional

import redis

from q_backend.storage.redis.client import get_redis

_TTL_SECONDS = 86400


def _counter_key(job_id: str) -> str:
    return f"job:fanin:{job_id}"


def _cancel_key(job_id: str) -> str:
    return f"job:cancel:{job_id}"


def init_counter(job_id: str, total: int, *, client: Optional[redis.Redis] = None) -> None:
    """Seed the fan-in counter for a job with the number of leaf units."""
    client = client or get_redis()
    client.set(_counter_key(job_id), total, ex=_TTL_SECONDS)


def decrement(job_id: str, *, client: Optional[redis.Redis] = None) -> int:
    """Atomically decrement the counter and return the remaining count."""
    client = client or get_redis()
    return int(client.decr(_counter_key(job_id)))


def decrement_and_is_last(job_id: str, *, client: Optional[redis.Redis] = None) -> bool:
    """Atomically decrement the counter; return True for the leaf that hits zero."""
    return decrement(job_id, client=client) <= 0


def set_cancelled(job_id: str, *, client: Optional[redis.Redis] = None) -> None:
    client = client or get_redis()
    client.set(_cancel_key(job_id), "1", ex=_TTL_SECONDS)


def is_cancelled(job_id: str, *, client: Optional[redis.Redis] = None) -> bool:
    client = client or get_redis()
    return client.exists(_cancel_key(job_id)) == 1


def clear_job_keys(job_id: str, *, client: Optional[redis.Redis] = None) -> None:
    """Remove fan-in/cancel bookkeeping once a job reaches a terminal state."""
    client = client or get_redis()
    client.delete(_counter_key(job_id), _cancel_key(job_id))
