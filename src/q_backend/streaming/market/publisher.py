"""Long-lived publisher from the Wine data gateway to ephemeral stream topics."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Mapping

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import _time_msc_to_naive_local
from q_backend.market_data.timezone import unix_seconds_to_brasilia_naive
from q_backend.streaming.market.arrow import bars_to_ipc, ticks_to_ipc
from q_backend.streaming.market.cursor import BarState, TickCursor, bar_transitions, new_ticks
from q_backend.streaming.publisher import EphemeralPublishError, EphemeralPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPublisherConfig:
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...] = ("M1",)
    tick_poll_interval_s: float = 0.25
    bar_poll_interval_s: float = 1.0
    tick_lookback_s: float = 5.0
    # Only the first bar request, or the first after a gateway outage, reaches back
    # this far: long enough to find the last bar across a weekend and a holiday.
    initial_bar_lookback: timedelta = timedelta(days=7)
    max_backoff_s: float = 30.0


@dataclass
class MarketDataPublisher:
    client: RemoteMt5Client
    publishers: Mapping[str, EphemeralPublisher]
    config: MarketPublisherConfig
    clock: Callable[[], datetime] = datetime.now
    tick_cursors: dict[str, TickCursor] = field(default_factory=dict)
    bar_states: dict[tuple[str, str], BarState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.config.symbols:
            raise ValueError("no symbols configured")

    def poll_ticks_once(self) -> int:
        published = 0
        now = self.clock()
        for symbol in self.config.symbols:
            cursor = self.tick_cursors.get(symbol, TickCursor())
            start = now - timedelta(seconds=self.config.tick_lookback_s)
            if cursor.last_msc is not None:
                start = _time_msc_to_naive_local(cursor.last_msc) - timedelta(seconds=1)
            columns = self.client.get_ticks_columnar(symbol, start, now, use_cache=False)
            fresh, advanced = new_ticks(cursor, columns)
            if len(fresh["time_msc"]) == 0:
                self.tick_cursors[symbol] = advanced
                continue
            self.publishers["quotes"].publish(
                routing_key={"symbol": symbol},
                payload_kind="arrow_ipc",
                payload_schema="schema/api/arrow/ticks.schema.json",
                payload=ticks_to_ipc(fresh),
            )
            # Advanced only after the publish, so a failed publish retries these ticks.
            self.tick_cursors[symbol] = advanced
            published += len(fresh["time_msc"])
        return published

    def poll_bars_once(self) -> int:
        published = 0
        now = self.clock()
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                key = (symbol, timeframe)
                previous = self.bar_states.get(key, BarState())
                # The forming bar onwards is all a transition needs; its open time
                # uses the same wall-clock convention as tick times.
                start = (
                    unix_seconds_to_brasilia_naive(previous.forming_time)
                    if previous.forming_time is not None
                    else now - self.config.initial_bar_lookback
                )
                columns = self.client.get_ohlcv_columnar(symbol, timeframe, start, now)
                completed, forming, state = bar_transitions(previous, columns)
                routing_key = {"symbol": symbol, "timeframe": timeframe}
                if completed is not None:
                    self.publishers["bars.completed"].publish(
                        routing_key=routing_key,
                        payload_kind="arrow_ipc",
                        payload_schema="schema/api/arrow/bars.schema.json",
                        payload=bars_to_ipc(completed),
                    )
                    published += 1
                if forming is not None:
                    self.publishers["bars.forming"].publish(
                        routing_key=routing_key,
                        payload_kind="arrow_ipc",
                        payload_schema="schema/api/arrow/bars.schema.json",
                        payload=bars_to_ipc(forming),
                    )
                    published += 1
                self.bar_states[key] = state
        return published

    def run_forever(self, stop: threading.Event) -> None:
        backoff = self.config.tick_poll_interval_s
        next_tick = next_bar = time.monotonic()
        unavailable: str | None = None
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_tick:
                    self.poll_ticks_once()
                    next_tick = now + self.config.tick_poll_interval_s
                if now >= next_bar:
                    self.poll_bars_once()
                    next_bar = now + self.config.bar_poll_interval_s
                if unavailable is not None:
                    logger.info("%s available; resuming live market-data publishing", unavailable)
                    unavailable = None
                backoff = self.config.tick_poll_interval_s
                stop.wait(min(0.05, max(0.0, min(next_tick, next_bar) - time.monotonic())))
                continue
            except ConnectionError:
                if unavailable != "gateway":
                    logger.warning("gateway unavailable; pausing live market-data publishing")
                    unavailable = "gateway"
                self.tick_cursors.clear()
                self.bar_states.clear()
            except EphemeralPublishError as exc:
                # Cursors were not advanced past the failed publish, so the same data
                # is retried once the stream is back.
                if unavailable != "stream":
                    logger.warning("stream unavailable; pausing live market-data publishing: %s", exc)
                    unavailable = "stream"
            except Exception:  # noqa: BLE001 - one bad poll must not end a long-lived publisher
                logger.exception("market-data poll failed; retrying")
            stop.wait(backoff)
            backoff = min(self.config.max_backoff_s, backoff * 2)
