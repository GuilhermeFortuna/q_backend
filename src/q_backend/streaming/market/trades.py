"""Per-symbol session trade ingestion: backfill, live polling and stream publication.

``TradeSessionCoordinator`` is the only writer of one symbol's session tape. It
serializes the full-session backfill and the live polling behind one ``step``, so the
history it holds and the batches it publishes are one ordered source sequence:

* Rows are identified by ``(provider, symbol, source_generation, time_msc, occurrence)``.
  Occurrence is assigned in provider order within *complete* millisecond groups, so equal
  looking prints are kept and a retry reproduces the same identities.
* The newest millisecond group is held until a later observation confirms its end (a row
  with a later millisecond, or the observed clock passing it by ``settle_ms``).
* Every poll re-reads the boundary group and validates it before appending anything. A
  shorter, reordered or changed boundary means the provider corrected its prefix: coverage
  turns partial and the session is recaptured under a new source generation.
* A failed publish never advances the cursor; the same rows are retried.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import numpy as np

from q_backend.market_data.clients.shared import TRADE_COLUMNS, TradeRange
from q_backend.streaming.market.arrow import trades_to_ipc
from q_backend.streaming.publisher import EphemeralPublishError, EphemeralPublisher

logger = logging.getLogger(__name__)

EXCHANGE_TIMEZONE = "America/Sao_Paulo"
CHUNK_MS = 60_000
PAGE_ROW_LIMIT = 50_000
BATCH_ROW_LIMIT = 4096
MAX_CHUNK_RETRIES = 5
UNAVAILABLE_RETRY_S = 30.0
STATUS_SCHEMA = "schema/stream/payloads/trade-source-status.schema.json"
TRADES_SCHEMA_PATH = "schema/api/arrow/trades.schema.json"

Columns = dict[str, np.ndarray]
CoverageState = Literal["complete", "partial", "unavailable"]
Phase = Literal["backfilling", "live", "unavailable"]


class TradeSource(Protocol):
    def get_trades(self, symbol: str, start_utc: datetime, end_utc: datetime) -> TradeRange: ...


class TradeSink(Protocol):
    """Where confirmed trades and coverage changes go; the stream in production."""

    def publish_trades(self, context: "TradeContext", columns: Columns) -> tuple[str, int]: ...

    def publish_status(self, status: Mapping[str, object]) -> tuple[str, int]: ...

    def watermark(self) -> tuple[str, int]: ...


@dataclass(frozen=True)
class TradeContext:
    provider_id: str
    symbol: str
    source_generation: str
    exchange_timezone: str
    session_key: str
    volume_field: str
    volume_unit: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "symbol": self.symbol,
            "source_generation": self.source_generation,
            "exchange_timezone": self.exchange_timezone,
            "session_key": self.session_key,
            "volume_field": self.volume_field,
            "volume_unit": self.volume_unit,
        }


@dataclass(frozen=True)
class Coverage:
    state: CoverageState
    reason: str | None
    covered_from: datetime | None
    covered_to: datetime | None


@dataclass
class Diagnostics:
    """Counters and timings for operators; published by the service, never a monitor."""

    backfill_rows: int = 0
    backfill_chunks: int = 0
    backfill_seconds: float = 0.0
    invalid_records: int = 0
    overlap_mismatches: int = 0
    retries: int = 0
    source_gaps: int = 0
    generations: int = 0
    last_poll_ms: float = 0.0

    def as_mapping(self) -> dict[str, float]:
        return {name: float(value) for name, value in self.__dict__.items()}


@dataclass(frozen=True)
class FrozenSession:
    """An immutable view of the confirmed session prefix and its live watermark."""

    context: TradeContext
    coverage: Coverage
    columns: Columns
    watermark: tuple[str, int]
    session_from: datetime
    session_to: datetime
    invalid_trade_count: int

    @property
    def trade_count(self) -> int:
        return len(self.columns["time_msc"])


@dataclass(frozen=True)
class BackfillProgress:
    state: str
    status_token: str
    covered_to: datetime | None
    rows: int


class _Recapture(Exception):
    """The source disagrees with what was ingested; rebuild under a new generation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def empty_columns() -> Columns:
    dtypes = {
        "time_msc": np.int64,
        "price": np.float64,
        "volume": np.float64,
        "volume_real": np.float64,
        "raw_flags": np.int32,
        "occurrence": np.int64,
    }
    return {name: np.zeros(0, dtype=dtypes[name]) for name in TRADE_COLUMNS}


def concat_columns(parts: list[Columns]) -> Columns:
    if not parts:
        return empty_columns()
    return {name: np.concatenate([part[name] for part in parts]) for name in TRADE_COLUMNS}


def take(columns: Mapping[str, np.ndarray], start: int, stop: int | None = None) -> Columns:
    return {name: columns[name][start:stop] for name in TRADE_COLUMNS}


def assign_occurrence(columns: Mapping[str, np.ndarray]) -> Columns:
    """Occurrence = position within a run of equal milliseconds, in provider order."""
    time_msc = columns["time_msc"]
    count = len(time_msc)
    result = {name: columns[name] for name in TRADE_COLUMNS}
    if count == 0:
        result["occurrence"] = np.zeros(0, dtype=np.int64)
        return result
    starts = np.flatnonzero(np.r_[True, time_msc[1:] != time_msc[:-1]])
    group_start = np.repeat(starts, np.diff(np.r_[starts, count]))
    result["occurrence"] = np.arange(count, dtype=np.int64) - group_start
    return result


def _same_rows(left: Mapping[str, np.ndarray], right: Mapping[str, np.ndarray]) -> bool:
    """Row-by-row equality of the identity-bearing values, treating NaN volume_real as equal."""
    return all(
        np.array_equal(left[name], right[name], equal_nan=name in ("price", "volume", "volume_real"))
        for name in ("time_msc", "price", "volume", "volume_real", "raw_flags")
    )


def session_bounds(now: datetime, tz: ZoneInfo) -> tuple[str, datetime, datetime]:
    """The exchange-local calendar day containing ``now`` as UTC bounds (a day key, not hours)."""
    local_day: date = now.astimezone(tz).date()
    start = datetime.combine(local_day, dtime.min, tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), dtime.min, tzinfo=tz)
    return local_day.isoformat(), start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


@dataclass
class TradeSessionCoordinator:
    client: TradeSource
    symbol: str
    sink: TradeSink
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    settle_ms: int = 2000
    chunk_ms: int = CHUNK_MS
    page_limit: int = PAGE_ROW_LIMIT
    batch_limit: int = BATCH_ROW_LIMIT
    max_retries: int = MAX_CHUNK_RETRIES
    retry_backoff_s: float = 1.0
    monotonic: Callable[[], float] = time.monotonic
    token_factory: Callable[[], str] = lambda: secrets.token_hex(6)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    def __post_init__(self) -> None:
        self._tz = ZoneInfo(EXCHANGE_TIMEZONE)
        self.phase: Phase = "backfilling"
        self.generation = ""
        self.session_key = ""
        self._session_start = self._session_end = self.clock()
        self._context: TradeContext | None = None
        self._coverage_state: CoverageState = "partial"
        self._coverage_reason: str | None = "backfill_in_progress"
        self._invalid = 0
        self._live_invalid = 0
        self._history: list[Columns] = []
        self._history_rows = 0
        self._boundary: Columns = empty_columns()
        self._boundary_ms: int | None = None
        self._scanned_to_ms = 0
        self._covered_to: datetime | None = None
        self._pending: list[tuple[int, int]] = []
        self._backfill_started = 0.0
        self._failures = 0
        self._retry_at = 0.0
        self._recapture_reason: str | None = None
        self._announced: tuple | None = None
        self._frozen_end_ms = 0
        self._begin_generation("startup")

    # -- public surface -----------------------------------------------------------------
    @property
    def backfilling(self) -> bool:
        return self.phase == "backfilling"

    def progress(self) -> BackfillProgress:
        return BackfillProgress(
            state=self.phase,
            status_token=f"backfill-{self.generation}",
            covered_to=self._covered_to,
            rows=self._history_rows,
        )

    def coverage(self) -> Coverage:
        return Coverage(self._coverage_state, self._coverage_reason, self._session_start, self._covered_to)

    def freeze(self) -> FrozenSession | None:
        """The confirmed prefix and live watermark, or None while no history is servable."""
        if self.phase != "live" or self._context is None:
            return None
        return FrozenSession(
            context=self._context,
            coverage=self.coverage(),
            columns=concat_columns(self._history),
            watermark=self.sink.watermark(),
            session_from=self._session_start,
            session_to=self._session_end,
            invalid_trade_count=self._invalid,
        )

    def step(self, *, budget_s: float = 0.5, may_backfill: bool = True) -> bool:
        """Advance backfill by chunks within ``budget_s`` or run one live poll; True if work ran."""
        now = self.monotonic()
        if now < self._retry_at:
            return False
        try:
            if self.phase == "backfilling":
                if not may_backfill:
                    return False
                return self._backfill(budget_s)
            if self.phase == "unavailable":
                raise _Recapture("retry_unavailable")
            return self._poll()
        except _Recapture as exc:
            self._begin_generation(exc.reason)
            return True
        except (ConnectionError, EphemeralPublishError) as exc:
            self._on_failure(exc)
            return True

    # -- generations ----------------------------------------------------------------------
    def _begin_generation(self, reason: str) -> None:
        now = self.clock()
        self.session_key, self._session_start, self._session_end = session_bounds(now, self._tz)
        self.generation = f"gen-{self.session_key}-{self.token_factory()}"
        self.diagnostics.generations += 1
        self.phase = "backfilling"
        self._context = None
        self._history, self._history_rows = [], 0
        self._boundary, self._boundary_ms = empty_columns(), None
        self._invalid = self._live_invalid = 0
        self._failures = 0
        self._retry_at = 0.0
        self._recapture_reason = None
        self._covered_to = None
        self._coverage_state = "partial"
        self._coverage_reason = "backfill_in_progress"
        self._last_meta = ("unknown", "volume", "provider-lots")
        self._held: Columns = empty_columns()
        start_ms, end_ms = _ms(self._session_start), _ms(self._session_end)
        self._frozen_end_ms = min(_ms(now) + 1, end_ms)
        self._scanned_to_ms = start_ms
        # Chunks are consumed from the end of the list so the earliest range is last.
        boundaries = list(range(start_ms, self._frozen_end_ms, self.chunk_ms)) + [self._frozen_end_ms]
        self._pending = [(a, b) for a, b in zip(boundaries, boundaries[1:])][::-1]
        self._backfill_started = self.monotonic()
        logger.info("trade session %s starting generation %s (%s)", self.symbol, self.generation, reason)
        self._announce(force=True)

    # -- backfill -------------------------------------------------------------------------
    def _backfill(self, budget_s: float) -> bool:
        deadline = self.monotonic() + budget_s
        worked = False
        while self._pending and self.monotonic() < deadline:
            start_ms, end_ms = self._pending[-1]
            result = self._fetch(start_ms, end_ms)
            if result is None:
                return True
            self._pending.pop()
            worked = True
            self.diagnostics.backfill_chunks += 1
            if self._split_if_dense(result, start_ms, end_ms):
                continue
            self._accept_backfill_chunk(result, start_ms, end_ms)
        if self.phase == "unavailable":
            return True
        if not self._pending:
            self._finish_backfill()
            return True
        return worked

    def _split_if_dense(self, result: TradeRange, start_ms: int, end_ms: int) -> bool:
        """Split a dense or truncated chunk on a millisecond edge, never inside a group."""
        dense = result.truncated or len(result) > self.page_limit
        if not dense or end_ms - start_ms <= 1:
            return False
        mid = start_ms + (end_ms - start_ms) // 2
        self._pending.extend([(mid, end_ms), (start_ms, mid)])
        return True

    def _accept_backfill_chunk(self, result: TradeRange, start_ms: int, end_ms: int) -> None:
        self._failures = 0
        if result.availability != "available":
            if result.coverage_reason == "symbol_not_found":
                self.phase = "unavailable"
                self._pending.clear()
                self._set_coverage("unavailable", "symbol_not_found")
                self._retry_at = self.monotonic() + UNAVAILABLE_RETRY_S
                self._announce()
                return
            self._record_gap(result.coverage_reason or "source_unavailable")
            return
        if result.truncated:
            # One millisecond holds more than the source can return whole: report it.
            self._record_gap("range_truncated")
            return
        columns = self._validated_rows(result, start_ms, end_ms)
        if columns is None:
            self._record_gap("invalid_source_order")
            return
        self._last_meta = (result.provider_id or "unknown", result.volume_field, result.volume_unit)
        self._adopt_context(result, len(columns["time_msc"]) > 0 or result.invalid_trade_count > 0)
        if result.invalid_trade_count:
            self._invalid += result.invalid_trade_count
            self.diagnostics.invalid_records += result.invalid_trade_count
            self._set_coverage("partial", "invalid_trade_records")
        if len(columns["time_msc"]):
            self._history_append(columns)
            self.diagnostics.backfill_rows += len(columns["time_msc"])
        self._scanned_to_ms = end_ms
        self._covered_to = _from_ms(end_ms)

    def _finish_backfill(self) -> None:
        # The newest group may still be open: keep it out of the confirmed history.
        rows = concat_columns(self._history)
        confirmed, held = self._split_confirmed(rows, self._frozen_end_ms)
        self._history = [confirmed] if len(confirmed["time_msc"]) else []
        self._history_rows = len(confirmed["time_msc"])
        self._held = held
        self._set_boundary(confirmed)
        self.diagnostics.backfill_seconds = self.monotonic() - self._backfill_started
        if self._context is None:
            # An empty session so far: the first trades confirm this context or reset it.
            self._context = self._tentative_context()
        self._covered_to = _from_ms(self._frozen_end_ms)
        if self._coverage_reason == "backfill_in_progress":
            self._set_coverage("complete", None)
        self.phase = "live"
        self._announce(force=True)
        logger.info(
            "trade backfill %s complete: %d rows, %d chunks, %.2fs, coverage=%s",
            self.symbol,
            self._history_rows,
            self.diagnostics.backfill_chunks,
            self.diagnostics.backfill_seconds,
            self._coverage_state,
        )

    # -- live polling ---------------------------------------------------------------------
    def _poll(self) -> bool:
        started = self.monotonic()
        now = self.clock()
        session_end_ms = _ms(self._session_end)
        clock_ms = _ms(now) + 1
        observed_end = min(clock_ms, session_end_ms)
        if self._boundary_ms is not None:
            from_ms = self._boundary_ms
        elif len(self._held["time_msc"]):
            from_ms = int(self._held["time_msc"][0])
        else:
            from_ms = self._scanned_to_ms
        if observed_end <= from_ms:
            if now >= self._session_end:
                raise _Recapture("session_rollover")
            return False
        result = self._fetch(from_ms, observed_end)
        if result is None:
            return True
        self._failures = 0
        if result.availability != "available" or result.truncated:
            reason = "range_truncated" if result.truncated else (result.coverage_reason or "source_unavailable")
            self._source_error(reason)
            return True
        if self._recapture_reason is not None:
            # The source answers again after an error: rebuild the whole session.
            raise _Recapture(self._recapture_reason)
        columns = self._validated_rows(result, from_ms, observed_end)
        if columns is None:
            self._source_error("invalid_source_order")
            return True
        if len(columns["time_msc"]) or result.invalid_trade_count:
            self._check_context(result)
        if result.invalid_trade_count:
            observed = self._invalid_floor() + result.invalid_trade_count
            self.diagnostics.invalid_records += max(0, observed - self._invalid)
            self._invalid = max(self._invalid, observed)
            self._live_invalid = max(self._live_invalid, result.invalid_trade_count)
            self._set_coverage("partial", "invalid_trade_records")
        new_rows = self._new_rows(columns)
        # Settling uses the unclamped clock: after the session ends every group is complete.
        confirmed, held = self._split_confirmed(new_rows, clock_ms)
        self._publish_confirmed(confirmed)
        self._held = held
        self._scanned_to_ms = observed_end
        self._covered_to = _from_ms(observed_end)
        self.diagnostics.last_poll_ms = (self.monotonic() - started) * 1000
        self._announce()
        if now >= self._session_end and not len(held["time_msc"]):
            raise _Recapture("session_rollover")
        return True

    def _invalid_floor(self) -> int:
        return self._invalid - self._live_invalid

    def _new_rows(self, columns: Columns) -> Columns:
        """Rows beyond what is already published: the validated overlap is dropped."""
        times = columns["time_msc"]
        if self._boundary_ms is None:
            return columns
        low = int(np.searchsorted(times, self._boundary_ms, side="left"))
        high = int(np.searchsorted(times, self._boundary_ms, side="right"))
        published = len(self._boundary["time_msc"])
        group = take(columns, low, high)
        if high - low < published or not _same_rows(take(group, 0, published), self._boundary):
            self.diagnostics.overlap_mismatches += 1
            self._set_coverage("partial", "overlap_mismatch")
            self._announce()
            raise _Recapture("overlap_mismatch")
        return take(columns, low + published)

    def _publish_confirmed(self, confirmed: Columns) -> None:
        count = len(confirmed["time_msc"])
        for start in range(0, count, self.batch_limit):
            batch = take(confirmed, start, start + self.batch_limit)
            assert self._context is not None
            # The cursor and history advance only after the publish succeeds.
            self.sink.publish_trades(self._context, batch)
            self._history_append(batch)
            self._set_boundary(batch)

    # -- shared helpers -------------------------------------------------------------------
    def _fetch(self, start_ms: int, end_ms: int) -> TradeRange | None:
        try:
            return self.client.get_trades(self.symbol, _from_ms(start_ms), _from_ms(end_ms))
        except ConnectionError:
            self.diagnostics.retries += 1
            self._failures += 1
            if self.phase == "backfilling" and self._failures >= self.max_retries and self._pending:
                self._pending.pop()
                self._failures = 0
                self._record_gap("source_unavailable")
            elif self.phase == "live":
                self._source_error("source_unavailable")
            self._retry_at = self.monotonic() + self.retry_backoff_s * min(2 ** max(0, self._failures - 1), 30)
            return None

    def _validated_rows(self, result: TradeRange, start_ms: int, end_ms: int) -> Columns | None:
        columns = {name: np.asarray(result.columns[name]) for name in TRADE_COLUMNS}
        times = columns["time_msc"]
        if len(times) == 0:
            return empty_columns()
        in_range = bool(times[0] >= start_ms and times[-1] < end_ms)
        if not in_range or bool(np.any(np.diff(times) < 0)):
            return None
        # The coordinator, not the gateway, owns occurrence numbering.
        return assign_occurrence(columns)

    def _adopt_context(self, result: TradeRange, has_rows: bool) -> None:
        """Freeze provider, field and unit from the first response that carries trades.

        An empty response says nothing about the volume field, so it never decides it.
        """
        if not has_rows:
            return
        if self._context is None:
            self._context = self._context_from(result)
            return
        if (result.volume_field, result.volume_unit) != (
            self._context.volume_field,
            self._context.volume_unit,
        ):
            raise _Recapture("volume_field_changed")

    def _check_context(self, result: TradeRange) -> None:
        assert self._context is not None
        if result.provider_id and result.provider_id != self._context.provider_id:
            raise _Recapture("provider_changed")
        if (result.volume_field, result.volume_unit) != (self._context.volume_field, self._context.volume_unit):
            raise _Recapture("volume_field_changed")

    def _context_from(self, result: TradeRange) -> TradeContext:
        return TradeContext(
            provider_id=result.provider_id,
            symbol=self.symbol,
            source_generation=self.generation,
            exchange_timezone=EXCHANGE_TIMEZONE,
            session_key=self.session_key,
            volume_field=result.volume_field,
            volume_unit=result.volume_unit,
        )

    def _tentative_context(self) -> TradeContext:
        provider, field_name, unit = self._last_meta
        return TradeContext(
            provider, self.symbol, self.generation, EXCHANGE_TIMEZONE, self.session_key, field_name, unit
        )

    def _split_confirmed(self, rows: Columns, observed_end_ms: int) -> tuple[Columns, Columns]:
        """Rows of complete groups, and the open newest group held back."""
        times = rows["time_msc"]
        if len(times) == 0:
            return rows, rows
        last = int(times[-1])
        if observed_end_ms - last >= self.settle_ms:
            return rows, take(rows, len(times))
        first_open = int(np.searchsorted(times, last, side="left"))
        return take(rows, 0, first_open), take(rows, first_open)

    def _history_append(self, columns: Columns) -> None:
        self._history.append(columns)
        self._history_rows += len(columns["time_msc"])

    def _set_boundary(self, columns: Columns) -> None:
        """The published rows at the newest published millisecond: the overlap to validate."""
        times = columns["time_msc"]
        if len(times) == 0:
            return
        last = int(times[-1])
        start = int(np.searchsorted(times, last, side="left"))
        group = take(columns, start)
        if self._boundary_ms == last:
            group = concat_columns([self._boundary, group])
        self._boundary, self._boundary_ms = group, last

    def _record_gap(self, reason: str) -> None:
        self.diagnostics.source_gaps += 1
        self._set_coverage("partial", reason)

    def _source_error(self, reason: str) -> None:
        """A source error after backfill: partial until the session is recaptured."""
        if self._recapture_reason is None:
            self.diagnostics.source_gaps += 1
            self._set_coverage("partial", reason)
            self._recapture_reason = f"recapture_after_{reason}"
            self._announce()

    def _on_failure(self, exc: Exception) -> None:
        self.diagnostics.retries += 1
        self._failures += 1
        self._retry_at = self.monotonic() + self.retry_backoff_s * min(2 ** max(0, self._failures - 1), 30)
        logger.warning("trade ingestion for %s will retry: %s", self.symbol, exc)

    def _set_coverage(self, state: CoverageState, reason: str | None) -> None:
        """Coverage only degrades within a generation, keeping the first reason."""
        severity = {"complete": 0, "partial": 1, "unavailable": 2}
        building = self._coverage_reason == "backfill_in_progress"
        if not building and severity[state] < severity[self._coverage_state]:
            return
        if not building and state == self._coverage_state and self._coverage_state != "complete":
            return
        self._coverage_state, self._coverage_reason = state, reason

    def _announce(self, force: bool = False) -> None:
        """Publish trades.status when the contracted status changes; failures retry later."""
        status = self._status_message()
        key = tuple(sorted((k, str(v)) for k, v in status.items() if k != "last_trade_watermark"))
        if not force and key == self._announced:
            return
        try:
            self.sink.publish_status(status)
        except EphemeralPublishError as exc:
            logger.warning("trade status for %s not published yet: %s", self.symbol, exc)
            return
        self._announced = key

    def _status_message(self) -> dict[str, object]:
        epoch, seq = self.sink.watermark()
        context = self._context or self._tentative_context()
        return {
            "provider_id": context.provider_id,
            "symbol": self.symbol,
            "source_generation": self.generation,
            "volume_field": context.volume_field,
            "volume_unit": context.volume_unit,
            "covered_from": iso_utc(self._session_start),
            "covered_to": iso_utc(self._covered_to),
            "coverage_state": self._coverage_state,
            "classification_coverage": "partial" if self._coverage_state != "unavailable" else "unavailable",
            "coverage_reason": self._coverage_reason,
            "last_trade_watermark": {"epoch": epoch, "seq": seq},
        }


def iso_utc(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if moment else None


class TradeStreamSink:
    """Publishes to the ``trades`` and ``trades.status`` topics and tracks the watermark."""

    def __init__(self, trades: EphemeralPublisher, status: EphemeralPublisher) -> None:
        self._trades = trades
        self._status = status
        self._watermark: tuple[str, int] | None = None

    def publish_trades(self, context: TradeContext, columns: Columns) -> tuple[str, int]:
        self._watermark = self._trades.publish(
            routing_key={"symbol": context.symbol},
            payload_kind="arrow_ipc",
            payload_schema=TRADES_SCHEMA_PATH,
            payload=trades_to_ipc(columns, context.as_mapping()),
        )
        return self._watermark

    def publish_status(self, status: Mapping[str, object]) -> tuple[str, int]:
        return self._status.publish(
            routing_key={"symbol": str(status["symbol"])},
            payload_kind="control",
            payload_schema=STATUS_SCHEMA,
            payload=dict(status),
        )

    def watermark(self) -> tuple[str, int]:
        """``(epoch, seq)`` of the newest published trades batch; seq 0 before the first."""
        if self._watermark is None:
            self._watermark = self._trades_position()
        return self._watermark

    def _trades_position(self) -> tuple[str, int]:
        client = self._trades.client
        from q_backend.streaming.keys import seq_key, topic_epoch_key

        epoch_raw = client.get(topic_epoch_key("trades"))
        if epoch_raw is None:
            # Create the epoch exactly as the publish script would, so consumers
            # can name it before any trade has been published.
            candidate = f"{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4)}"
            if client.set(topic_epoch_key("trades"), candidate, nx=True):
                client.delete(seq_key("trades"))
            epoch_raw = client.get(topic_epoch_key("trades"))
        seq_raw = client.get(seq_key("trades"))
        epoch = epoch_raw.decode() if isinstance(epoch_raw, bytes) else str(epoch_raw)
        return epoch, int(seq_raw or 0)


__all__ = [
    "BATCH_ROW_LIMIT",
    "BackfillProgress",
    "Coverage",
    "Diagnostics",
    "EXCHANGE_TIMEZONE",
    "FrozenSession",
    "PAGE_ROW_LIMIT",
    "TradeContext",
    "TradeSessionCoordinator",
    "TradeStreamSink",
    "assign_occurrence",
    "concat_columns",
    "empty_columns",
    "iso_utc",
    "session_bounds",
]
