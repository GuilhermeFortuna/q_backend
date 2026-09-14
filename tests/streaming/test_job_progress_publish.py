import json
from unittest.mock import patch
import fakeredis
import pytest

from q_backend.storage.redis.progress import set_job_progress
from q_backend.streaming.keys import stream_key


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


def _read_progress_stream(fake_redis):
    raw_entries = fake_redis.xrange(stream_key("jobs.progress"))
    entries = []
    for _msg_id, fields in raw_entries:
        raw_h = fields.get("h") or fields.get(b"h")
        raw_p = fields.get("p") or fields.get(b"p")
        header = json.loads(raw_h) if isinstance(raw_h, (str, bytes)) else raw_h
        payload = json.loads(raw_p) if isinstance(raw_p, (str, bytes)) else raw_p
        entries.append((header, payload))
    return entries


def test_backtest_running_payload(fake_redis):
    from q_backend.streaming.jobs import publish_job_progress

    publish_job_progress("backtest", "b-123", {"status": "running"}, client=fake_redis)

    entries = _read_progress_stream(fake_redis)
    assert len(entries) == 1
    header, payload = entries[0]
    assert header["key"] == {"kind": "backtest", "job_id": "b-123"}
    assert payload == {
        "kind": "backtest",
        "job_id": "b-123",
        "status": "running",
        "progress": None,
    }


def test_terminal_status_publishes_nothing(fake_redis):
    from q_backend.streaming.jobs import publish_job_progress

    publish_job_progress("backtest", "b-123", {"status": "completed"}, client=fake_redis)
    publish_job_progress("backtest", "b-123", {"status": "error"}, client=fake_redis)
    publish_job_progress("backtest", "b-123", {"status": "failed"}, client=fake_redis)
    publish_job_progress("backtest", "b-123", {"status": "cancelled"}, client=fake_redis)

    entries = _read_progress_stream(fake_redis)
    assert len(entries) == 0


def test_optimization_namespace_job_publishes_kind_optimization(fake_redis):
    set_job_progress(fake_redis, "opt-456", {"status": "running", "progress": 0.5}, namespace="job")

    entries = _read_progress_stream(fake_redis)
    assert len(entries) == 1
    header, payload = entries[0]
    assert header["key"] == {"kind": "optimization", "job_id": "opt-456"}
    assert payload["kind"] == "optimization"
    assert payload["status"] == "running"
    assert payload["progress"] == 0.5


def test_neural_text_progress_publishes_null_progress_with_message(fake_redis):
    from q_backend.streaming.jobs import publish_job_progress

    publish_job_progress(
        "neural_training",
        "n-789",
        {"status": "running", "progress": "Epoch 3/10"},
        client=fake_redis,
    )

    entries = _read_progress_stream(fake_redis)
    assert len(entries) == 1
    _header, payload = entries[0]
    assert payload["kind"] == "neural_training"
    assert payload["progress"] is None
    assert payload["message"] == "Epoch 3/10"


def test_connection_error_does_not_raise(fake_redis):
    from q_backend.streaming.jobs import publish_job_progress

    with patch(
        "q_backend.streaming.publisher.EphemeralPublisher.publish",
        side_effect=ConnectionError("Redis unreachable"),
    ):
        publish_job_progress("backtest", "b-err", {"status": "running"}, client=fake_redis)


def test_job_flagged_terminal_publishes_nothing(fake_redis):
    from q_backend.streaming.jobs import flag_job_terminal, publish_job_progress

    flag_job_terminal(fake_redis, "backtest", "b-term")

    publish_job_progress("backtest", "b-term", {"status": "running"}, client=fake_redis)

    entries = _read_progress_stream(fake_redis)
    assert len(entries) == 0
