import fakeredis
import pytest

from q_backend.streaming.codec import routing_key_string
from q_backend.streaming.keys import latest_key
from q_backend.streaming.publisher import EphemeralPublisher


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis()


def test_durable_topic_raises_value_error(fake_redis):
    with pytest.raises(ValueError, match="not a declared ephemeral topic"):
        EphemeralPublisher(fake_redis, "jobs.terminal", producer_id="test-p")


def test_undeclared_topic_raises_value_error(fake_redis):
    with pytest.raises(ValueError, match="not a declared ephemeral topic"):
        EphemeralPublisher(fake_redis, "unknown.topic", producer_id="test-p")


def test_publisher_sequence_and_epoch_reset(fake_redis):
    publisher = EphemeralPublisher(fake_redis, "jobs.progress", producer_id="worker-1")

    payload_1 = {
        "job_id": "job-1",
        "kind": "backtest",
        "status": "running",
        "progress": 0.1,
    }
    payload_dict = payload_1
    epoch_1, seq_1 = publisher.publish(
        routing_key={"kind": "backtest", "job_id": "job-1"},
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_dict,
    )
    assert seq_1 == 1
    assert len(epoch_1) > 0

    # Three publishes return 1, 2, 3 with one epoch
    epoch_2, seq_2 = publisher.publish(
        routing_key={"kind": "backtest", "job_id": "job-1"},
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_dict,
    )
    assert seq_2 == 2
    assert epoch_2 == epoch_1

    epoch_3, seq_3 = publisher.publish(
        routing_key={"kind": "backtest", "job_id": "job-1"},
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_dict,
    )
    assert seq_3 == 3
    assert epoch_3 == epoch_1

    # After flushall the next publish returns seq 1 and a different epoch
    fake_redis.flushall()

    epoch_4, seq_4 = publisher.publish(
        routing_key={"kind": "backtest", "job_id": "job-1"},
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_dict,
    )
    assert seq_4 == 1
    assert epoch_4 != epoch_1


def test_latest_hash_interleaved_keys(fake_redis):
    publisher = EphemeralPublisher(fake_redis, "jobs.progress", producer_id="worker-1")

    key_a = {"kind": "backtest", "job_id": "job-A"}
    key_b = {"kind": "backtest", "job_id": "job-B"}
    payload_a1 = {"job_id": "job-A", "kind": "backtest", "status": "running", "progress": 0.1}
    payload_b1 = {"job_id": "job-B", "kind": "backtest", "status": "running", "progress": 0.2}
    payload_a2 = {"job_id": "job-A", "kind": "backtest", "status": "running", "progress": 0.9}

    # Interleaved publishes: A, B, A
    publisher.publish(
        routing_key=key_a,
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_a1,
    )
    publisher.publish(
        routing_key=key_b,
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_b1,
    )
    publisher.publish(
        routing_key=key_a,
        payload_kind="control",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
        payload=payload_a2,
    )

    rk_a = routing_key_string("jobs.progress", key_a)
    rk_b = routing_key_string("jobs.progress", key_b)

    id_a = fake_redis.hget(latest_key("jobs.progress"), rk_a)
    id_b = fake_redis.hget(latest_key("jobs.progress"), rk_b)

    assert id_a is not None
    assert id_b is not None

    # Verify latest hash for key A points at the third entry (seq 3)
    entry_a = fake_redis.xrange("q:stream:jobs.progress", min=id_a, max=id_a)
    assert len(entry_a) == 1
    entry_id, fields = entry_a[0]
    assert entry_id == id_a
    assert b'progress":0.9' in fields[b"p"] or b"0.9" in fields[b"p"]

    # Verify latest hash for key B points at the second entry (seq 2)
    entry_b = fake_redis.xrange("q:stream:jobs.progress", min=id_b, max=id_b)
    assert len(entry_b) == 1
    assert b'progress":0.2' in entry_b[0][1][b"p"] or b"0.2" in entry_b[0][1][b"p"]
