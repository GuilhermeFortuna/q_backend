"""TradeSessionCoordinator: backfill, live overlap, retries and source corrections."""

from __future__ import annotations

import numpy as np
import pytest

from q_backend.streaming.market.trades import TradeSessionCoordinator, concat_columns
from tests.streaming.trade_fakes import (
    SESSION_START,
    T0,
    T0_MS,
    FakeClock,
    FakeSink,
    FakeTradeSource,
    Mono,
    identities,
    row,
)

HOUR_MS = 3_600_000


def _coordinator(source, sink=None, clock=None, **kwargs):
    sink = sink or FakeSink()
    clock = clock or FakeClock()
    kwargs.setdefault("chunk_ms", HOUR_MS)
    kwargs.setdefault("monotonic", Mono())
    kwargs.setdefault("retry_backoff_s", 0.0)
    coordinator = TradeSessionCoordinator(source, "WINZ26", sink, clock=clock, **kwargs)
    return coordinator, sink, clock


def _backfill(coordinator, limit=2000):
    for _ in range(limit):
        if not coordinator.backfilling:
            return
        coordinator.step(budget_s=60)
    raise AssertionError("backfill did not finish")


def _step(coordinator, times=1):
    for _ in range(times):
        coordinator.step()


def _tape(columns):
    return list(zip(columns["time_msc"].tolist(), columns["price"].tolist(), columns["occurrence"].tolist()))


def test_backfill_then_live_equals_one_ordered_source_sequence():
    rows = [row(-5000, 100.0), row(-4000, 101.0), row(-4000, 101.0), row(-4000, 101.0), row(-500, 102.0)]
    source = FakeTradeSource(rows)
    coordinator, sink, clock = _coordinator(source)

    _backfill(coordinator)
    frozen = coordinator.freeze()
    # Backfill published nothing: the confirmed prefix lives in the immutable history.
    assert sink.batches == []
    # The newest group (-500) may still be growing, so it is held out of the prefix.
    assert identities(frozen.columns) == [(T0_MS - 5000, 0), (T0_MS - 4000, 0), (T0_MS - 4000, 1), (T0_MS - 4000, 2)]
    assert frozen.watermark == ("epoch-1", 0)
    assert frozen.coverage.state == "complete"

    # The cut group grows, then a later millisecond confirms its end.
    source.rows += [row(-500, 102.0), row(-500, 102.0), row(500, 103.0), row(500, 103.0)]
    clock.advance(seconds=1)
    _step(coordinator)

    assert identities(sink.published()) == [(T0_MS - 500, 0), (T0_MS - 500, 1), (T0_MS - 500, 2)]
    assert coordinator.freeze().trade_count == 7  # the +500 group is still open

    clock.advance(seconds=5)
    _step(coordinator)
    everything = concat_columns([frozen.columns, sink.published()])
    assert identities(everything) == identities_of_source(source.rows)
    assert coordinator.freeze().watermark == ("epoch-1", sink.seq)


def identities_of_source(rows):
    counts: dict[int, int] = {}
    result = []
    for r in sorted(rows, key=lambda r: r[0]):
        result.append((r[0], counts.get(r[0], 0)))
        counts[r[0]] = counts.get(r[0], 0) + 1
    return result


def test_dense_ranges_split_without_dropping_same_millisecond_records():
    rows = [row(-5000 + i // 4) for i in range(40)]  # four equal prints per millisecond
    source = FakeTradeSource(rows)
    coordinator, _sink, _clock = _coordinator(source, chunk_ms=HOUR_MS, page_limit=7)

    _backfill(coordinator)

    assert identities(coordinator.freeze().columns) == identities_of_source(rows)
    # Splitting stops at a single millisecond: a group is fetched whole, however large.
    assert any(end - start == 1 for start, end in source.calls)


def test_a_millisecond_group_the_source_cannot_hold_marks_the_range_incomplete():
    rows = [row(-4000) for _ in range(5)] + [row(-9000)]
    source = FakeTradeSource(rows)
    source.truncate_over = 3
    coordinator, _sink, _clock = _coordinator(source, page_limit=3)

    _backfill(coordinator)

    frozen = coordinator.freeze()
    assert frozen.coverage.state == "partial" and frozen.coverage.reason == "range_truncated"
    assert frozen.trade_count == 1  # the oversize group is reported missing, never truncated


def test_poll_overlap_appends_late_occurrences_and_never_doubles():
    source = FakeTradeSource([row(-9000), row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    clock.advance(seconds=1)
    _step(coordinator)
    assert identities(sink.published()) == []  # backfill already holds the confirmed group

    source.rows.append(row(-9000))  # a late print in an already confirmed millisecond
    source.rows.append(row(500))
    clock.advance(seconds=1)
    _step(coordinator)
    _step(coordinator)  # a repeated poll re-reads the same overlap

    assert identities(sink.published()) == [(T0_MS - 9000, 2), (T0_MS + 500, 0)]
    assert identities(coordinator.freeze().columns) == [
        (T0_MS - 9000, 0),
        (T0_MS - 9000, 1),
        (T0_MS - 9000, 2),
        (T0_MS + 500, 0),
    ]


def test_failed_publish_keeps_the_cursor_and_retries_the_same_identities():
    source = FakeTradeSource([row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    source.rows += [row(0), row(0), row(100)]
    clock.advance(seconds=1)

    sink.fail_trades = 1
    _step(coordinator)
    assert sink.batches == []
    assert coordinator.freeze().trade_count == 1

    _step(coordinator)
    assert identities(sink.published()) == [(T0_MS, 0), (T0_MS, 1), (T0_MS + 100, 0)]
    _step(coordinator, 3)
    assert identities(sink.published()) == [(T0_MS, 0), (T0_MS, 1), (T0_MS + 100, 0)]


def test_partial_batch_failure_resumes_inside_a_group_without_gaps_or_duplicates():
    source = FakeTradeSource([row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100, batch_limit=2)
    _backfill(coordinator)
    source.rows += [row(0) for _ in range(5)]
    clock.advance(seconds=1)

    original = sink.publish_trades
    calls = {"n": 0}

    def flaky(context, columns):
        calls["n"] += 1
        if calls["n"] == 2:
            sink.fail_trades = 1
        return original(context, columns)

    sink.publish_trades = flaky
    _step(coordinator)
    assert len(sink.published()["time_msc"]) == 2
    sink.publish_trades = original
    _step(coordinator, 2)

    assert identities(sink.published()) == [(T0_MS, i) for i in range(5)]


def test_source_correction_changes_generation_and_recaptures_the_session():
    source = FakeTradeSource([row(-9000, 100.0), row(-9000, 100.0), row(-8000, 101.0)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first = coordinator.generation
    source.rows[2] = row(-8000, 555.0)  # the provider corrected a confirmed print
    clock.advance(seconds=1)

    _step(coordinator)  # validates the boundary, finds the change
    assert any(s["coverage_state"] == "partial" and s["coverage_reason"] == "overlap_mismatch" for s in sink.statuses)
    assert coordinator.backfilling and coordinator.generation != first
    _backfill(coordinator)

    frozen = coordinator.freeze()
    assert frozen.context.source_generation == coordinator.generation != first
    assert _tape(frozen.columns)[-1] == (T0_MS - 8000, 555.0, 0)
    assert frozen.coverage.state == "complete"
    assert coordinator.diagnostics.overlap_mismatches == 1


@pytest.mark.parametrize("mutate", ["shrink", "reorder"])
def test_shrinking_or_reordered_overlap_forces_recapture(mutate):
    source = FakeTradeSource([row(-9000, 100.0), row(-9000, 101.0)])
    coordinator, _sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first = coordinator.generation
    if mutate == "shrink":
        source.rows.pop()
    else:
        source.rows.reverse()
        source.rows = [(r[0], *r[1:]) for r in source.rows]
    clock.advance(seconds=1)

    _step(coordinator)

    assert coordinator.generation != first


def test_volume_field_change_starts_a_new_generation_instead_of_converting():
    source = FakeTradeSource([row(-9000, real=2.0)], field="volume_real", unit="contracts")
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first = coordinator.generation
    assert coordinator.freeze().context.volume_field == "volume_real"

    source.field, source.unit = "volume", "provider-lots"
    source.rows.append(row(500))
    clock.advance(seconds=1)
    _step(coordinator)

    assert coordinator.generation != first and coordinator.backfilling
    _backfill(coordinator)
    assert coordinator.freeze().context.volume_field == "volume"
    assert sink.batches == []


def test_empty_response_never_decides_the_volume_field():
    source = FakeTradeSource([], field="volume", unit="provider-lots")
    coordinator, _sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first = coordinator.generation
    source.field, source.unit = "volume_real", "contracts"
    source.rows.append(row(500, real=3.0))
    clock.advance(seconds=1)
    _step(coordinator)
    # The empty session had only a tentative field, so the first trades reset it once.
    _backfill(coordinator)

    frozen = coordinator.freeze()
    assert coordinator.generation != first
    assert frozen.context.volume_field == "volume_real"
    assert frozen.context.volume_unit == "contracts"


def test_invalid_records_and_source_gaps_never_report_complete():
    source = FakeTradeSource([row(-9000), row(-8000)])
    source.invalid_in_range = 2
    coordinator, _sink, _clock = _coordinator(source)
    _backfill(coordinator)
    frozen = coordinator.freeze()
    assert frozen.coverage.state == "partial" and frozen.coverage.reason == "invalid_trade_records"
    assert frozen.invalid_trade_count >= 2 and coordinator.diagnostics.invalid_records >= 2


def test_gateway_outage_during_backfill_retries_then_reports_a_gap():
    source = FakeTradeSource([row(-9000)])
    source.fail_next = 3
    coordinator, _sink, _clock = _coordinator(source, max_retries=5)
    _backfill(coordinator)
    assert coordinator.freeze().coverage.state == "complete"
    assert coordinator.diagnostics.retries == 3

    source = FakeTradeSource([row(-9000)])
    source.fail_next = 10_000
    coordinator, _sink, _clock = _coordinator(source, max_retries=2)
    _backfill(coordinator, limit=5000)
    frozen = coordinator.freeze()
    assert frozen.coverage.state == "partial" and frozen.coverage.reason == "source_unavailable"
    assert coordinator.diagnostics.source_gaps >= 1


def test_live_source_error_marks_partial_announces_status_and_recaptures_once_healthy():
    source = FakeTradeSource([row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first = coordinator.generation
    count = len(sink.statuses)

    source.fail_next = 1
    clock.advance(seconds=1)
    _step(coordinator)
    assert len(sink.statuses) == count + 1
    assert sink.statuses[-1]["coverage_state"] == "partial"
    assert sink.statuses[-1]["source_generation"] == first
    assert coordinator.generation == first

    _step(coordinator)  # the source answers again: rebuild the session
    assert coordinator.generation != first
    _backfill(coordinator)
    assert coordinator.freeze().coverage.state == "complete"


def test_truncated_or_unavailable_live_responses_are_not_complete():
    source = FakeTradeSource([row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    source.unavailable_calls = 1
    clock.advance(seconds=1)
    _step(coordinator)
    assert sink.statuses[-1]["coverage_state"] == "partial"
    assert sink.statuses[-1]["coverage_reason"] == "source_error"


def test_unknown_symbol_is_unavailable_not_empty_complete():
    source = FakeTradeSource([])
    source.unknown_symbol = True
    coordinator, sink, _clock = _coordinator(source)
    _backfill(coordinator)
    assert coordinator.freeze() is None
    assert coordinator.phase == "unavailable"
    assert sink.statuses[-1]["coverage_state"] == "unavailable"


def test_status_publish_failure_does_not_block_ingestion_and_is_retried():
    source = FakeTradeSource([row(-9000)])
    sink = FakeSink()
    sink.fail_status = 2
    coordinator, sink, clock = _coordinator(source, sink=sink, settle_ms=100)
    _backfill(coordinator)
    source.rows.append(row(0))
    clock.advance(seconds=1)
    _step(coordinator)
    assert identities(sink.published()) == [(T0_MS, 0)]
    _step(coordinator)
    assert sink.statuses and sink.statuses[-1]["coverage_state"] == "complete"


def test_session_rollover_starts_a_new_generation_for_the_next_exchange_day():
    source = FakeTradeSource([row(-9000)])
    coordinator, _sink, clock = _coordinator(source, settle_ms=100)
    _backfill(coordinator)
    first, key = coordinator.generation, coordinator.session_key
    assert key == "2026-10-01"

    clock.now = SESSION_START.replace(day=2, hour=3, minute=0, second=1)  # just after local midnight
    _step(coordinator, 2)

    assert coordinator.session_key == "2026-10-02" and coordinator.generation != first


def test_session_day_uses_exchange_local_midnight_not_utc():
    source = FakeTradeSource([])
    clock = FakeClock(T0.replace(hour=2, minute=30))  # 23:30 local on 09-30
    coordinator, _sink, _clock = _coordinator(source, clock=clock)
    assert coordinator.session_key == "2026-09-30"
    assert coordinator.freeze() is None
    _backfill(coordinator)
    assert coordinator.freeze().session_from.isoformat() == "2026-09-30T03:00:00+00:00"


def test_trades_publish_in_bounded_batches():
    source = FakeTradeSource([row(-9000)])
    coordinator, sink, clock = _coordinator(source, settle_ms=100, batch_limit=3)
    _backfill(coordinator)
    source.rows += [row(i) for i in range(7)]
    clock.advance(seconds=1)
    _step(coordinator)

    sizes = [len(cols["time_msc"]) for _ctx, cols, _pos in sink.batches]
    assert sizes == [3, 3, 1] and max(sizes) <= 3
    assert np.all(np.diff(sink.published()["time_msc"]) >= 0)
