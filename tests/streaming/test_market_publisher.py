from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import fakeredis
import numpy as np
import pyarrow as pa
import pytest

from q_backend.streaming.market.publisher import MarketDataPublisher, MarketPublisherConfig
from q_backend.market_data.timezone import unix_seconds_to_brasilia_naive
from q_backend.streaming.publisher import EphemeralPublishError, EphemeralPublisher


def _publishers(redis):
    return {
        topic: EphemeralPublisher(redis, topic, producer_id="test-market")
        for topic in ("quotes", "bars.forming", "bars.completed")
    }


def _bars(times, closes):
    count = len(times)
    return {
        "time": np.asarray(times, dtype=np.int64),
        "open": np.ones(count),
        "high": np.ones(count),
        "low": np.ones(count),
        "close": np.asarray(closes),
        "tick_volume": np.ones(count, dtype=np.int64),
        "spread": np.zeros(count, dtype=np.int64),
        "real_volume": np.zeros(count, dtype=np.int64),
    }


class _Client:
    def __init__(self):
        self.tick_calls = 0
        self.bar_calls = 0

    def get_ticks_columnar(self, *_args, **kwargs):
        assert kwargs["use_cache"] is False
        self.tick_calls += 1
        return {
            "time_msc": np.asarray([1_726_131_600_000, 1_726_131_600_000, 1_726_131_600_001]),
            "bid": np.asarray([1.0, 2.0, 3.0]),
            "ask": np.asarray([1.1, 2.1, 3.1]),
            "last": np.asarray([1.05, 2.05, 3.05]),
            "volume": np.ones(3),
            "flags": np.ones(3, dtype=np.int32),
        }

    def get_ohlcv_columnar(self, *_args):
        self.bar_calls += 1
        return _bars([10], [1.0]) if self.bar_calls == 1 else _bars([10, 20], [1.0, 2.0])


def test_publisher_deduplicates_ticks_and_orders_completed_before_new_forming():
    redis = fakeredis.FakeRedis()
    publisher = MarketDataPublisher(
        _Client(), _publishers(redis), MarketPublisherConfig(symbols=("WIN$N",)), lambda: datetime(2024, 9, 1, 10)
    )
    assert publisher.poll_ticks_once() == 3
    assert publisher.poll_ticks_once() == 0
    quote = redis.xrange("q:stream:quotes")[0][1]
    header = json.loads(quote[b"h"])
    assert header["key"] == {"symbol": "WIN$N"}
    assert pa.ipc.open_stream(quote[b"p"]).read_all().num_rows == 3

    publisher.poll_bars_once()
    publisher.poll_bars_once()
    completed = redis.xrange("q:stream:bars.completed")[0][1]
    forming = redis.xrange("q:stream:bars.forming")[-1][1]
    assert json.loads(completed[b"h"])["key"] == {"symbol": "WIN$N", "timeframe": "M1"}
    assert int(completed[b"h"] != forming[b"h"]) == 1


class _RecordingClient(_Client):
    def __init__(self):
        super().__init__()
        self.bar_starts = []

    def get_ohlcv_columnar(self, _symbol, _timeframe, start, _end):
        self.bar_starts.append(start)
        return super().get_ohlcv_columnar()


def test_bar_polls_after_the_first_request_only_the_forming_bar_onwards():
    """Re-fetching two days of bars every second loads the gateway for the last two rows."""
    client = _RecordingClient()
    now = datetime(2024, 9, 1, 10)
    publisher = MarketDataPublisher(
        client, _publishers(fakeredis.FakeRedis()), MarketPublisherConfig(symbols=("WIN$N",)), lambda: now
    )

    publisher.poll_bars_once()
    publisher.poll_bars_once()

    assert client.bar_starts[0] <= now - MarketPublisherConfig.initial_bar_lookback
    assert client.bar_starts[1] == unix_seconds_to_brasilia_naive(10)


class _FlakyPublisher(EphemeralPublisher):
    failures = 1

    def publish(self, **kwargs):
        if _FlakyPublisher.failures:
            _FlakyPublisher.failures -= 1
            raise EphemeralPublishError("redis down")
        return super().publish(**kwargs)


def test_a_failed_publish_does_not_advance_the_tick_cursor():
    """Advancing before the publish succeeds loses the ticks of every failed poll."""
    redis = fakeredis.FakeRedis()
    publishers = _publishers(redis)
    publishers["quotes"] = _FlakyPublisher(redis, "quotes", producer_id="test-market")
    publisher = MarketDataPublisher(
        _Client(), publishers, MarketPublisherConfig(symbols=("WIN$N",)), lambda: datetime(2024, 9, 1, 10)
    )

    with pytest.raises(EphemeralPublishError):
        publisher.poll_ticks_once()
    assert publisher.poll_ticks_once() == 3


def test_run_forever_survives_stream_and_gateway_errors(caplog):
    """Only ConnectionError was handled; a Redis outage or a gateway 400 ended the process."""

    class Client(_Client):
        def get_ohlcv_columnar(self, *args):
            if self.bar_calls == 0:
                self.bar_calls += 1
                raise ValueError("Bad request to /v1/ohlcv.")
            return super().get_ohlcv_columnar(*args)

    redis = fakeredis.FakeRedis()
    publishers = _publishers(redis)
    publishers["quotes"] = _FlakyPublisher(redis, "quotes", producer_id="test-market")
    _FlakyPublisher.failures = 1
    client = Client()
    publisher = MarketDataPublisher(
        client,
        publishers,
        MarketPublisherConfig(
            symbols=("WIN$N",), tick_poll_interval_s=0.01, bar_poll_interval_s=0.01, max_backoff_s=0.02
        ),
        lambda: datetime(2024, 9, 1, 10),
    )
    stop = threading.Event()
    worker = threading.Thread(target=publisher.run_forever, args=(stop,))

    worker.start()
    deadline = time.monotonic() + 5
    while (redis.xlen("q:stream:quotes") == 0 or client.bar_calls < 2) and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(5)

    assert not worker.is_alive()
    assert redis.xlen("q:stream:quotes") == 1
    assert client.bar_calls >= 2
