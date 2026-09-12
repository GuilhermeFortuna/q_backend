"""Tests for shared symbol/timeframe bar fetching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from q_backend.execution.bar_coordinator import (
    BarCoordinator,
    BarStreamKey,
    DeploymentBarConsumer,
)
from q_backend.market_data.models import OHLCV


class _FakeProvider:
    def __init__(self, bars: list[OHLCV]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]:
        self.calls.append((symbol, timeframe, start, end))
        return list(self._bars)


def _bar(open_time: datetime, close: float) -> OHLCV:
    return OHLCV(
        time=open_time,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        tick_volume=100,
    )


def test_two_deployments_share_one_fetch():
    base = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(hours=i), 100.0 + i) for i in range(5)]
    provider = _FakeProvider(bars)
    now = base + timedelta(hours=6)
    coordinator = BarCoordinator(provider=provider, clock=lambda: now)

    consumers = [
        DeploymentBarConsumer("d1", "WIN$", "H1", last_evaluated_close=base + timedelta(hours=2)),
        DeploymentBarConsumer("d2", "WIN$", "H1", last_evaluated_close=base + timedelta(hours=1)),
    ]
    batches = coordinator.poll(consumers, initial_window_bars=10)
    assert len(batches) == 1
    assert coordinator.fetch_count == 1
    assert BarStreamKey("WIN$", "H1") in batches


def test_new_bars_for_consumer_respects_watermark():
    base = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(hours=i), 100.0 + i) for i in range(4)]
    provider = _FakeProvider(bars)
    now = base + timedelta(hours=5)
    coordinator = BarCoordinator(provider=provider, clock=lambda: now)
    consumer = DeploymentBarConsumer("d1", "WIN$", "H1")
    batch = coordinator.poll([consumer], initial_window_bars=10)[BarStreamKey("WIN$", "H1")]

    watermark = base + timedelta(hours=2)  # close of second bar
    consumer = DeploymentBarConsumer("d1", "WIN$", "H1", last_evaluated_close=watermark)
    fresh = coordinator.new_bars_for_consumer(batch, consumer)
    assert len(fresh) == 2
