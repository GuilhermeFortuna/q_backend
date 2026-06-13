"""Staging area for fan-out leaf results, backed by a Redis hash.

Each leaf actor stashes its serialized partial result under the job's hash keyed by
the leaf index. When the fan-in counter hits zero, the finalizer loads every partial
and aggregates them into the final job result. Keyed by index so a retried leaf
overwrites rather than duplicates.
"""

import json
from typing import Any, Optional

import redis

from q_backend.storage.redis.client import get_redis

_TTL_SECONDS = 86400


def _key(job_id: str) -> str:
    return f"job:partials:{job_id}"


def stash_partial(
    job_id: str,
    index: int,
    payload: dict[str, Any],
    *,
    client: Optional[redis.Redis] = None,
) -> None:
    client = client or get_redis()
    key = _key(job_id)
    client.hset(key, str(index), json.dumps(payload))
    client.expire(key, _TTL_SECONDS)


def load_partials(
    job_id: str, *, client: Optional[redis.Redis] = None
) -> list[dict[str, Any]]:
    """Return all stashed partials (order unspecified; callers sort as needed)."""
    client = client or get_redis()
    raw = client.hgetall(_key(job_id))
    return [json.loads(value) for value in raw.values()]


def clear_partials(job_id: str, *, client: Optional[redis.Redis] = None) -> None:
    client = client or get_redis()
    client.delete(_key(job_id))
