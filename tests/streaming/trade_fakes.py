"""Deterministic fakes for the session trade ingestion tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from q_backend.market_data.clients.shared import TradeRange
from q_backend.streaming.market.trades import TradeContext, concat_columns, empty_columns
from q_backend.streaming.publisher import EphemeralPublishError

T0 = datetime(2026, 10, 1, 15, 0, 0, tzinfo=timezone.utc)  # 12:00 in America/Sao_Paulo
T0_MS = int(T0.timestamp() * 1000)
SESSION_START = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class Mono:
    """Monotonic stand-in that never blocks retries unless a test sets a floor."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


def row(offset_ms: int, price: float = 100.0, volume: float = 1.0, flags: int = 8 | 16, real: float | None = None):
    return (T0_MS + offset_ms, price, volume, np.nan if real is None else real, flags)


class FakeTradeSource:
    """A provider whose tape the test mutates between observations."""

    def __init__(self, rows=(), *, field: str = "volume", unit: str = "provider-lots", provider: str = "fake-mt5"):
        self.rows = list(rows)
        self.field, self.unit, self.provider = field, unit, provider
        self.calls: list[tuple[int, int]] = []
        self.fail_next = 0
        self.truncate_over: int | None = None
        self.invalid_in_range = 0
        self.unknown_symbol = False
        self.unavailable_calls = 0

    def get_trades(self, symbol, start_utc, end_utc):
        start, end = int(start_utc.timestamp() * 1000), int(end_utc.timestamp() * 1000)
        self.calls.append((start, end))
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("gateway down")
        if self.unknown_symbol:
            return self._range(symbol, [], start_utc, end_utc, availability="unavailable", reason="symbol_not_found")
        if self.unavailable_calls:
            self.unavailable_calls -= 1
            return self._range(symbol, [], start_utc, end_utc, availability="unavailable", reason="source_error")
        selected = [r for r in self.rows if start <= r[0] < end]
        if self.truncate_over is not None and len(selected) > self.truncate_over:
            return self._range(symbol, [], start_utc, end_utc, truncated=True)
        invalid = self.invalid_in_range if selected else 0
        return self._range(symbol, selected, start_utc, end_utc, invalid=invalid)

    def _range(self, symbol, rows, start, end, *, availability="available", reason=None, truncated=False, invalid=0):
        columns = _columns(rows)
        complete = availability == "available" and not truncated and not invalid
        if invalid and availability == "available" and not truncated:
            reason = "invalid_trade_records"
        if truncated:
            reason = "range_truncated"
        return TradeRange(
            columns=columns,
            provider_id=self.provider,
            symbol=symbol,
            source_generation="gateway-test",
            volume_field=self.field,
            volume_unit=self.unit,
            availability=availability,
            range_complete=complete,
            covered_from_utc=start if complete else None,
            covered_to_utc=end if complete else None,
            coverage_reason=reason,
            invalid_trade_count=invalid,
            truncated=truncated,
        )


def _columns(rows):
    if not rows:
        return empty_columns()
    return {
        "time_msc": np.array([r[0] for r in rows], dtype=np.int64),
        "price": np.array([r[1] for r in rows], dtype=np.float64),
        "volume": np.array([r[2] for r in rows], dtype=np.float64),
        "volume_real": np.array([r[3] for r in rows], dtype=np.float64),
        "raw_flags": np.array([r[4] for r in rows], dtype=np.int32),
        # The provider's own numbering is ignored by the coordinator.
        "occurrence": np.zeros(len(rows), dtype=np.int64),
    }


class FakeSink:
    """Collects deliveries like the stream would, with an injectable publish failure."""

    def __init__(self) -> None:
        self.batches: list[tuple[TradeContext, dict, tuple[str, int]]] = []
        self.statuses: list[dict] = []
        self.fail_trades = 0
        self.fail_status = 0
        self.seq = 0

    def publish_trades(self, context, columns):
        if self.fail_trades:
            self.fail_trades -= 1
            raise EphemeralPublishError("redis down")
        self.seq += 1
        position = ("epoch-1", self.seq)
        self.batches.append((context, {k: v.copy() for k, v in columns.items()}, position))
        return position

    def publish_status(self, status):
        if self.fail_status:
            self.fail_status -= 1
            raise EphemeralPublishError("redis down")
        self.statuses.append(dict(status))
        return ("status-epoch", len(self.statuses))

    def watermark(self):
        return ("epoch-1", self.seq)

    def published(self, generation: str | None = None) -> dict:
        parts = [cols for ctx, cols, _ in self.batches if generation in (None, ctx.source_generation)]
        return concat_columns(parts)


def identities(columns) -> list[tuple[int, int]]:
    return list(zip(columns["time_msc"].tolist(), columns["occurrence"].tolist()))
