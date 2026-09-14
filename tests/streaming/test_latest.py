import fakeredis
import numpy as np
import pyarrow as pa
import pytest

from q_backend.streaming.market.arrow import ticks_to_ipc
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.snapshot import StreamUnavailable, latest_to_response, read_latest
from tests.streaming.replay_schema import assert_valid_replay


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=False)


def _quote_batch(close_values: list[float]) -> bytes:
    count = len(close_values)
    return ticks_to_ipc(
        {
            "time_msc": np.arange(count, dtype=np.int64),
            "bid": np.asarray(close_values, dtype=np.float64),
            "ask": np.asarray([v + 0.1 for v in close_values], dtype=np.float64),
            "last": np.asarray(close_values, dtype=np.float64),
            "volume": np.ones(count),
            "flags": np.ones(count, dtype=np.int32),
        }
    )


def test_read_latest_interleaved_symbols_and_key_filter(fake_redis):
    publisher = EphemeralPublisher(fake_redis, "quotes", producer_id="test-market")

    a1 = _quote_batch([1.0, 2.0, 3.0])
    b1 = _quote_batch([4.0, 5.0, 6.0])
    a2 = _quote_batch([7.0, 8.0, 9.0])

    publisher.publish(
        routing_key={"symbol": "A"},
        payload_kind="arrow_ipc",
        payload_schema="schema/api/arrow/ticks.schema.json",
        payload=a1,
    )
    publisher.publish(
        routing_key={"symbol": "B"},
        payload_kind="arrow_ipc",
        payload_schema="schema/api/arrow/ticks.schema.json",
        payload=b1,
    )
    _, a2_seq = publisher.publish(
        routing_key={"symbol": "A"},
        payload_kind="arrow_ipc",
        payload_schema="schema/api/arrow/ticks.schema.json",
        payload=a2,
    )

    latest = read_latest(fake_redis, "quotes")
    assert set(latest.keys()) == {"A", "B"}
    assert latest["A"].seq == a2_seq
    assert latest["B"].seq == 2

    from q_backend.streaming.codec import decode_entry
    from q_backend.streaming.keys import latest_key, stream_key

    stream_id = fake_redis.hget(latest_key("quotes"), "A")
    _, fields = fake_redis.xrange(stream_key("quotes"), min=stream_id, max=stream_id)[0]
    _, payload_bytes = decode_entry(fields)
    table = pa.ipc.open_stream(payload_bytes).read_all()
    assert table.num_rows == 3
    assert table.column("bid").to_pylist() == [7.0, 8.0, 9.0]

    only_b = read_latest(fake_redis, "quotes", key="B")
    assert set(only_b.keys()) == {"B"}

    response = latest_to_response("quotes", latest)
    assert_valid_replay("latest", response)


def test_read_latest_stream_unavailable(monkeypatch):
    client = fakeredis.FakeRedis()

    def _raise_ping():
        from redis.exceptions import RedisError

        raise RedisError("connection refused")

    monkeypatch.setattr(client, "ping", _raise_ping)
    with pytest.raises(StreamUnavailable):
        read_latest(client, "quotes")
