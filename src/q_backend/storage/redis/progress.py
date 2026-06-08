import json
from typing import Any

import redis

DEFAULT_PROGRESS_TTL_SECONDS = 86400


def _progress_key(job_id: str) -> str:
    return f"job:progress:{job_id}"


def set_job_progress(
    client: redis.Redis,
    job_id: str,
    payload: dict[str, Any],
    ttl_seconds: int = DEFAULT_PROGRESS_TTL_SECONDS,
) -> None:
    client.set(_progress_key(job_id), json.dumps(payload), ex=ttl_seconds)


def get_job_progress(client: redis.Redis, job_id: str) -> dict[str, Any] | None:
    raw = client.get(_progress_key(job_id))
    if raw is None:
        return None
    return json.loads(raw)


def delete_job_progress(client: redis.Redis, job_id: str) -> None:
    client.delete(_progress_key(job_id))
