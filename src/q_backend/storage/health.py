from typing import Literal, NotRequired, TypedDict

from sqlalchemy import text

from q_backend.storage.db.engine import get_engine
from q_backend.storage.redis.client import get_redis

StorageCheckStatus = Literal["ok", "error"]


class StorageCheckResult(TypedDict):
    status: StorageCheckStatus
    error: NotRequired[str]


def check_postgres() -> StorageCheckResult:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def check_redis() -> StorageCheckResult:
    try:
        get_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def storage_status() -> dict[str, StorageCheckResult]:
    return {"postgres": check_postgres(), "redis": check_redis()}
