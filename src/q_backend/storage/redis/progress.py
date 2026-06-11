import json
from typing import Any

import redis

DEFAULT_PROGRESS_TTL_SECONDS = 86400


def _progress_key(job_id: str, namespace: str = "job") -> str:
    return f"{namespace}:progress:{job_id}"


def set_job_progress(
    client: redis.Redis,
    job_id: str,
    payload: dict[str, Any],
    ttl_seconds: int = DEFAULT_PROGRESS_TTL_SECONDS,
    *,
    namespace: str = "job",
) -> None:
    client.set(_progress_key(job_id, namespace), json.dumps(payload), ex=ttl_seconds)


def get_job_progress(
    client: redis.Redis,
    job_id: str,
    *,
    namespace: str = "job",
) -> dict[str, Any] | None:
    raw = client.get(_progress_key(job_id, namespace))
    if raw is None:
        return None
    return json.loads(raw)


def delete_job_progress(
    client: redis.Redis,
    job_id: str,
    *,
    namespace: str = "job",
) -> None:
    client.delete(_progress_key(job_id, namespace))
