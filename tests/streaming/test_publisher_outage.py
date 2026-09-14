import time
import pytest
import redis
from q_backend.streaming.jobs import (
    _bounded_client,
    get_publisher_client,
    publish_job_progress,
    set_publisher_client,
)
from tests.streaming.test_job_events import ALL_JOB_KINDS, _get_terminal_events, _run_kind


def test_publishing_leaves_the_callers_client_unchanged() -> None:
    """The caller's client is shared with other Redis work, which must keep its own timeouts."""
    slow_client = redis.Redis(
        host="127.0.0.1",
        port=6399,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        retry_on_timeout=True,
    )
    before = dict(slow_client.connection_pool.connection_kwargs)

    publish_job_progress("backtest", "job-test-bound", {"status": "running", "progress": 0.5}, client=slow_client)

    assert slow_client.connection_pool.connection_kwargs == before


def test_publishing_through_a_slow_client_is_still_bounded() -> None:
    """A progress update must not stall a job for the caller's multi-second Redis timeouts."""
    unroutable = redis.Redis(host="10.255.255.1", port=6380, socket_timeout=5.0, socket_connect_timeout=5.0)

    started = time.monotonic()
    publish_job_progress("backtest", "job-test-slow", {"status": "running"}, client=unroutable)

    assert time.monotonic() - started < 1.0


def test_bounded_publisher_clients_are_reused() -> None:
    """A new pool per progress update opens a new connection for every update."""
    first = redis.Redis(host="127.0.0.1", port=6399)
    second = redis.Redis(host="127.0.0.1", port=6399)

    assert _bounded_client(first) is _bounded_client(second)
    assert get_publisher_client() is get_publisher_client()


@pytest.mark.parametrize("kind", ALL_JOB_KINDS)
def test_publisher_closed_port_all_kinds_complete(
    kind: str,
    run_jobs_sync,
    monkeypatch,
) -> None:
    """With the stream publisher pointed at a closed port, every job kind still

    completes under the harness, the time it loses to the dead publisher stays
    within budget, and its terminal event is committed to the outbox.

    Only time spent in Redis commands against the closed port is measured. The
    job's own compute scales with host speed and load (a shared 4-vCPU runner
    takes ~2x as long as a dev box), so a whole-job wall-clock budget is flaky
    without saying anything about publisher stalls.
    """
    closed_port = 6399
    bad_client = redis.Redis(host="127.0.0.1", port=closed_port)
    set_publisher_client(bad_client)

    stalled = {"seconds": 0.0, "calls": 0}
    original_execute = redis.Redis.execute_command

    def timed_execute(self, *args, **kwargs):
        if self.connection_pool.connection_kwargs.get("port") != closed_port:
            return original_execute(self, *args, **kwargs)
        started = time.monotonic()
        try:
            return original_execute(self, *args, **kwargs)
        finally:
            stalled["seconds"] += time.monotonic() - started
            stalled["calls"] += 1

    monkeypatch.setattr(redis.Redis, "execute_command", timed_execute)

    publisher_budget_seconds = 2.0
    job_id = _run_kind(kind, run_jobs_sync, monkeypatch)

    assert stalled["calls"] > 0, f"Job kind '{kind}' never reached the closed-port publisher; test is vacuous."
    assert stalled["seconds"] < publisher_budget_seconds, (
        f"Job kind '{kind}' spent {stalled['seconds']:.2f}s in {stalled['calls']} Redis calls to a closed "
        f"port, exceeding budget of {publisher_budget_seconds}s."
    )

    events = _get_terminal_events(run_jobs_sync.harness_session_factory, kind, job_id)
    assert len(events) == 1, f"Expected 1 terminal event for {kind}:{job_id}, found {len(events)}"
    assert events[0].payload["status"] == "completed"
