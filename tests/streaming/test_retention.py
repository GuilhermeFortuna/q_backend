import dataclasses
import pytest

from q_backend.streaming.keys import latest_key, seq_key, stream_key, topic_epoch_key
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.redis_binary import get_binary_redis
from q_contracts.topics import TOPICS


@pytest.mark.integration
def test_publisher_approximate_retention_trim(monkeypatch):
    topic = "jobs.progress"
    retention = 500
    custom_policy = dataclasses.replace(TOPICS[topic], retention_entries=retention)
    monkeypatch.setitem(TOPICS, topic, custom_policy)

    client = get_binary_redis()
    keys = [seq_key(topic), topic_epoch_key(topic), stream_key(topic), latest_key(topic)]
    client.delete(*keys)

    try:
        publisher = EphemeralPublisher(client, topic, producer_id="test-trim")
        total_to_publish = 2 * retention

        for i in range(total_to_publish):
            publisher.publish(
                routing_key={"kind": "backtest", "job_id": f"job-{i % 10}"},
                payload_kind="control",
                payload_schema="schema/stream/payloads/job-progress.schema.json",
                payload={
                    "job_id": f"job-{i % 10}",
                    "kind": "backtest",
                    "status": "running",
                    "progress": 0.5,
                },
            )

        xlen = client.xlen(stream_key(topic))
        assert xlen <= int(retention * 1.1)
        assert xlen >= retention
    finally:
        client.delete(*keys)
