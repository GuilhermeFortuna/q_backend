"""Clock continuity, deterministic ordering and the exclusive end of the market timeline."""

from __future__ import annotations

import pytest

from q_backend.replay.config import to_market_us
from q_backend.replay.journal import QueueLimitError, ReplayRunStore
from q_backend.replay.timeline import BackdatingError, ReplayClock, ReplayTimeline

from .conftest import START, make_config

BASE = to_market_us(START)
SEC = 1_000_000


def _seed_run(store: ReplayRunStore, end_seconds: int = 60) -> ReplayTimeline:
    config = make_config(end_seconds=end_seconds)
    with store.transaction() as conn:
        store.create_run(
            conn,
            run_id="run",
            generation="gen",
            state="running",
            rate="1.0",
            cursor_us=BASE,
            end_us=config.end_us,
            config_hash=config.config_hash(),
            config_json=config.canonical_json(),
            dataset_checksums=(),
            max_queued_events=config.limits.max_queued_events,
            max_journal_bytes=config.limits.max_journal_bytes,
        )
    return ReplayTimeline(store)


def _collect(processed: list[tuple[str, int]]):
    def handler(event, conn):
        processed.append((event.kind, event.at_us))

    return handler


def test_clock_advances_from_monotonic_anchor_at_rate() -> None:
    clock = ReplayClock.start(rate=2.0, market_us=BASE, mono_ns=1_000, paused=False)
    assert clock.now_us(mono_ns=1_000) == BASE
    assert clock.now_us(mono_ns=1_000 + 500_000_000) == BASE + SEC  # 0.5 s real = 1 s market


def test_paused_clock_is_frozen_while_real_time_passes() -> None:
    clock = ReplayClock.start(rate=1.0, market_us=BASE, mono_ns=0, paused=True)
    assert clock.now_us(mono_ns=10 * SEC * 1000) == BASE


def test_rate_change_preserves_current_market_time() -> None:
    clock = ReplayClock.start(rate=1.0, market_us=BASE, mono_ns=0, paused=False)
    reached = clock.now_us(mono_ns=3 * SEC * 1000)  # 3 s real at 1x
    clock.reanchor(market_us=reached, mono_ns=3 * SEC * 1000, rate=10.0, paused=False)
    assert clock.now_us(mono_ns=3 * SEC * 1000) == reached
    assert clock.now_us(mono_ns=4 * SEC * 1000) == reached + 10 * SEC


def test_non_monotonic_real_time_never_runs_market_backwards() -> None:
    clock = ReplayClock.start(rate=1.0, market_us=BASE, mono_ns=5_000, paused=False)
    assert clock.now_us(mono_ns=1_000) == BASE


def test_events_run_in_market_time_priority_then_sequence_order(store: ReplayRunStore) -> None:
    timeline = _seed_run(store)
    seen: list[tuple[str, int]] = []
    t = BASE + SEC
    with store.transaction() as conn:
        # Recorded market move scheduled first, but expiry (priority 0) at the same instant
        # must run before it, and the response (priority 9) after the execution (priority 4).
        timeline.schedule(conn, at_us=t, priority=9, kind="response", payload={}, max_queued_events=50)
        timeline.schedule(conn, at_us=t, priority=1, kind="market", payload={}, max_queued_events=50)
        timeline.schedule(conn, at_us=t, priority=0, kind="expiry", payload={}, max_queued_events=50)
        timeline.schedule(conn, at_us=t, priority=4, kind="execution", payload={}, max_queued_events=50)
        timeline.schedule(conn, at_us=BASE + 2 * SEC, priority=5, kind="later", payload={}, max_queued_events=50)
        timeline.advance_to(
            conn,
            target_us=t,
            end_us=to_market_us(START) + 3600 * SEC,
            handler=_collect(seen),
            max_journal_bytes=10**9,
        )
    assert [kind for kind, _ in seen] == ["expiry", "market", "execution", "response"]


def test_exclusive_end_prevents_execution_at_the_bound(store: ReplayRunStore) -> None:
    config_end = BASE + 10 * SEC
    timeline = _seed_run(store, end_seconds=10)
    seen: list[tuple[str, int]] = []
    with store.transaction() as conn:
        timeline.schedule(conn, at_us=config_end, priority=4, kind="execution-at-end", payload={}, max_queued_events=50)
        timeline.schedule(
            conn, at_us=config_end - 1, priority=4, kind="last-microsecond", payload={}, max_queued_events=50
        )
        timeline.advance_to(
            conn,
            target_us=config_end + SEC,
            end_us=config_end,
            handler=_collect(seen),
            max_journal_bytes=10**9,
        )
        assert timeline.cursor_us(conn) == config_end
    assert seen == [("last-microsecond", config_end - 1)]
    with store.transaction() as conn:
        pending = store.next_due(conn, config_end + SEC)
    assert pending is not None and pending.kind == "execution-at-end"


def test_zero_delay_event_scheduled_by_handler_runs_before_next_command(store: ReplayRunStore) -> None:
    timeline = _seed_run(store)
    seen: list[str] = []

    def handler(event, conn):
        seen.append(event.kind)
        if event.kind == "submit":
            timeline.schedule(conn, at_us=event.at_us, priority=4, kind="execution", payload={}, max_queued_events=50)

    with store.transaction() as conn:
        timeline.schedule(conn, at_us=BASE + SEC, priority=3, kind="submit", payload={}, max_queued_events=50)
        timeline.advance_to(
            conn, target_us=BASE + SEC, end_us=BASE + 60 * SEC, handler=handler, max_journal_bytes=10**9
        )
    assert seen == ["submit", "execution"]


def test_backdated_schedule_is_refused(store: ReplayRunStore) -> None:
    timeline = _seed_run(store)
    with store.transaction() as conn:
        timeline.advance_to(
            conn, target_us=BASE + 5 * SEC, end_us=BASE + 60 * SEC, handler=lambda e, c: None, max_journal_bytes=10**9
        )
        with pytest.raises(BackdatingError):
            timeline.schedule(conn, at_us=BASE + SEC, priority=0, kind="late", payload={}, max_queued_events=50)


def test_queue_limit_refuses_additional_events(store: ReplayRunStore) -> None:
    timeline = _seed_run(store)
    with store.transaction() as conn:
        timeline.schedule(conn, at_us=BASE, priority=0, kind="a", payload={}, max_queued_events=2)
        timeline.schedule(conn, at_us=BASE, priority=0, kind="b", payload={}, max_queued_events=2)
        with pytest.raises(QueueLimitError):
            timeline.schedule(conn, at_us=BASE, priority=0, kind="c", payload={}, max_queued_events=2)


def test_queue_with_duplicate_timestamps_keeps_every_record(store: ReplayRunStore) -> None:
    timeline = _seed_run(store)
    seen: list[str] = []
    with store.transaction() as conn:
        for name in ("tick-a", "tick-b", "tick-a"):
            timeline.schedule(conn, at_us=BASE, priority=1, kind=name, payload={}, max_queued_events=50)
        timeline.advance_to(
            conn,
            target_us=BASE + 1,
            end_us=BASE + 60 * SEC,
            handler=lambda e, c: seen.append(e.kind),
            max_journal_bytes=10**9,
        )
    assert seen == ["tick-a", "tick-b", "tick-a"]
