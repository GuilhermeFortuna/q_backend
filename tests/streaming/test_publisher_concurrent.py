import json
import multiprocessing as mp
import pytest

from q_backend.streaming.keys import latest_key, seq_key, stream_key, topic_epoch_key
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.redis_binary import get_binary_redis


def _publish_worker(worker_id: int, count: int) -> None:
    client = get_binary_redis()
    publisher = EphemeralPublisher(client, "jobs.progress", producer_id=f"worker-{worker_id}")
    for i in range(count):
        publisher.publish(
            routing_key={"kind": "backtest", "job_id": f"job-{worker_id}"},
            payload_kind="control",
            payload_schema="schema/stream/payloads/job-progress.schema.json",
            payload={
                "job_id": f"job-{worker_id}",
                "kind": "backtest",
                "status": "running",
                "progress": float(i) / count,
            },
        )


@pytest.mark.integration
def test_concurrent_publishing_gapless_order():
    topic = "jobs.progress"
    client = get_binary_redis()
    keys = [seq_key(topic), topic_epoch_key(topic), stream_key(topic), latest_key(topic)]
    client.delete(*keys)

    try:
        processes = [mp.Process(target=_publish_worker, args=(w, 100)) for w in range(8)]
        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30)
            assert not p.is_alive(), "Worker process timed out"
            assert p.exitcode == 0, f"Worker process exited with code {p.exitcode}"

        entries = client.xrange(stream_key(topic))
        assert len(entries) == 800

        seqs = [json.loads(fields[b"h"])["seq"] for _, fields in entries]
        assert seqs == list(range(1, 801))
        assert min(seqs) == 1
        assert max(seqs) == 800
    finally:
        client.delete(*keys)
