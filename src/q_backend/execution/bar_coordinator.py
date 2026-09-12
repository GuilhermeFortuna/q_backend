"""Completed-bar fetch coordination across deployments sharing symbol/timeframe."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol

import pandas as pd

from q_backend.execution.bars import (
    bar_close_time,
    drop_forming_bar,
    frame_close_times,
    ohlcv_list_to_frame,
)
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.models import OHLCV


class OhlcvProvider(Protocol):
    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]: ...


@dataclass(frozen=True)
class BarStreamKey:
    symbol: str
    timeframe: str


@dataclass(frozen=True)
class DeploymentBarConsumer:
    deployment_id: str
    symbol: str
    timeframe: str
    last_evaluated_close: datetime | None = None


@dataclass
class CompletedBarBatch:
    key: BarStreamKey
    frame: pd.DataFrame
    fetched_at: datetime
    fetch_span_start: datetime
    fetch_span_end: datetime

    @property
    def completed_close_times(self) -> pd.DatetimeIndex:
        if self.frame.empty:
            return pd.DatetimeIndex([])
        return frame_close_times(self.frame.index, self.key.timeframe)


@dataclass
class BarCoordinator:
    """Groups consumers by ``(symbol, timeframe)`` and shares one OHLCV fetch."""

    provider: OhlcvProvider
    clock: Callable[[], datetime] = field(default_factory=lambda: datetime.now)
    overlap_bars: int = 1
    fetch_count: int = field(default=0, init=False)

    def poll(
        self,
        consumers: list[DeploymentBarConsumer],
        *,
        initial_window_bars: int | None = None,
    ) -> dict[BarStreamKey, CompletedBarBatch]:
        now = self.clock()
        grouped: dict[BarStreamKey, list[DeploymentBarConsumer]] = {}
        for consumer in consumers:
            key = BarStreamKey(consumer.symbol, consumer.timeframe.upper())
            grouped.setdefault(key, []).append(consumer)

        batches: dict[BarStreamKey, CompletedBarBatch] = {}
        for key, group in grouped.items():
            batch = self._fetch_stream(
                key,
                group,
                now=now,
                initial_window_bars=initial_window_bars,
            )
            if batch is not None:
                batches[key] = batch
        return batches

    def new_bars_for_consumer(
        self,
        batch: CompletedBarBatch,
        consumer: DeploymentBarConsumer,
    ) -> pd.DataFrame:
        """Return only rows whose close is strictly after the consumer watermark."""
        frame = batch.frame
        if frame.empty:
            return frame
        closes = batch.completed_close_times
        if consumer.last_evaluated_close is None:
            return frame
        mask = closes > pd.Timestamp(consumer.last_evaluated_close)
        if not mask.any():
            return frame.iloc[0:0]
        return frame.loc[mask]

    def _fetch_stream(
        self,
        key: BarStreamKey,
        consumers: list[DeploymentBarConsumer],
        *,
        now: datetime,
        initial_window_bars: int | None,
    ) -> CompletedBarBatch | None:
        duration = bar_duration(key.timeframe)
        earliest_close = min(
            (c.last_evaluated_close for c in consumers if c.last_evaluated_close),
            default=None,
        )

        if earliest_close is None:
            if initial_window_bars is None:
                raise ValueError("initial_window_bars is required when no consumer has a watermark")
            end = now
            start = now - duration * initial_window_bars
        else:
            start = earliest_close - duration * self.overlap_bars
            end = now

        raw = self.provider.get_ohlcv(key.symbol, key.timeframe, start, end)
        self.fetch_count += 1
        frame = ohlcv_list_to_frame(raw)
        frame = drop_forming_bar(frame, key.timeframe, now=now)
        if frame.empty:
            return CompletedBarBatch(
                key=key,
                frame=frame,
                fetched_at=now,
                fetch_span_start=start,
                fetch_span_end=end,
            )

        # Completed bars only: drop any row whose close is still in the future.
        closes = frame_close_times(frame.index, key.timeframe)
        complete_mask = closes <= pd.Timestamp(now)
        frame = frame.loc[complete_mask]
        return CompletedBarBatch(
            key=key,
            frame=frame,
            fetched_at=now,
            fetch_span_start=start,
            fetch_span_end=end,
        )

    @staticmethod
    def close_time_for_row(row_open_time: datetime, timeframe: str) -> datetime:
        return bar_close_time(row_open_time, timeframe)
