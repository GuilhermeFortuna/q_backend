from q_backend.storage.redis.progress import (
    DEFAULT_PROGRESS_TTL_SECONDS,
    delete_job_progress,
    get_job_progress,
    set_job_progress,
)


def test_job_progress_round_trip(redis_client):
    payload = {"status": "running", "completed": 3, "total": 10}
    set_job_progress(redis_client, "job-1", payload)
    assert get_job_progress(redis_client, "job-1") == payload


def test_job_progress_delete(redis_client):
    set_job_progress(redis_client, "job-2", {"status": "done"})
    delete_job_progress(redis_client, "job-2")
    assert get_job_progress(redis_client, "job-2") is None


def test_job_progress_ttl(redis_client):
    set_job_progress(redis_client, "job-3", {"status": "running"}, ttl_seconds=60)
    ttl = redis_client.ttl("job:progress:job-3")
    assert 0 < ttl <= 60


def test_default_ttl_constant():
    assert DEFAULT_PROGRESS_TTL_SECONDS == 86400
