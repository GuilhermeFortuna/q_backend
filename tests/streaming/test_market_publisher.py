from __future__ import annotations

import json
from datetime import datetime

import fakeredis
import numpy as np
import pyarrow as pa

from q_backend.streaming.market.publisher import MarketDataPublisher, MarketPublisherConfig
from q_backend.streaming.publisher import EphemeralPublisher


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
