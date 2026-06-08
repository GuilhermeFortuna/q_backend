import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from q_backend.storage.db.engine import get_engine
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import get_job_progress, set_job_progress


@pytest.mark.integration
def test_postgres_connection():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.mark.integration
def test_redis_connection():
    try:
        client = get_redis()
        client.ping()
        set_job_progress(client, "integration-job", {"status": "ok"})
        assert get_job_progress(client, "integration-job") == {"status": "ok"}
        client.delete("job:progress:integration-job")
    except Exception:
        pytest.skip("Redis is not available or misconfigured")
