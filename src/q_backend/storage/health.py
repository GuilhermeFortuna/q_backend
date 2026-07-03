import logging
from typing import Literal, NotRequired, TypedDict

from sqlalchemy import text

from q_backend.storage.db.engine import get_engine
from q_backend.storage.redis.client import get_redis

logger = logging.getLogger(__name__)

StorageCheckStatus = Literal["ok", "error"]


class StorageCheckResult(TypedDict):
    status: StorageCheckStatus
    error: NotRequired[str]


def check_postgres() -> StorageCheckResult:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - health probe reports any failure as status
        # Best-effort probe: the whole point is to report connectivity, so any
        # failure becomes an "error" status (surfaced to the caller) and is logged.
        logger.warning("Postgres health check failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


def check_redis() -> StorageCheckResult:
    try:
        get_redis().ping()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - health probe reports any failure as status
        # Best-effort probe: any failure becomes an "error" status (surfaced to
        # the caller) and is logged.
        logger.warning("Redis health check failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


def storage_status() -> dict[str, StorageCheckResult]:
    return {"postgres": check_postgres(), "redis": check_redis()}
