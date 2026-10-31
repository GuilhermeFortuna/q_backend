"""Canonical market timeline: rate-scaled market time and deterministic event processing.

Market time advances from a monotonic anchor at an exact rational rate, so changing the rate or
pausing never rewrites past event times. Real-time lease and timeout checks never use this clock.
Events are processed in ``(market time, priority, sequence)`` order. The exclusive run end is a
hard bound: nothing at or after it is processed, including an event scheduled exactly at it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from q_backend.replay.journal import QueuedEvent, ReplayRunStore

EventHandler = Callable[[QueuedEvent, sqlite3.Connection], None]
_NS_PER_US = 1000


class BackdatingError(ValueError):
    """An event was scheduled before the current canonical cursor."""


@dataclass
class ReplayClock:
    """Market time as a function of a monotonic clock: ``anchor + elapsed * rate``, or frozen."""

    rate: Fraction
    anchor_market_us: int
    anchor_mono_ns: int
    paused: bool

    @classmethod
    def start(cls, *, rate: float, market_us: int, mono_ns: int, paused: bool) -> ReplayClock:
        return cls(
            rate=Fraction(str(rate)),
            anchor_market_us=market_us,
            anchor_mono_ns=mono_ns,
            paused=paused,
        )

    def now_us(self, mono_ns: int) -> int:
        if self.paused:
            return self.anchor_market_us
        elapsed_ns = max(0, mono_ns - self.anchor_mono_ns)
        scaled = elapsed_ns * self.rate.numerator
        return self.anchor_market_us + scaled // (self.rate.denominator * _NS_PER_US)

    def reanchor(self, *, market_us: int, mono_ns: int, rate: float, paused: bool) -> None:
        """Continue from an explicit market instant at a (possibly new) rate and pause state."""
        self.anchor_market_us = market_us
        self.anchor_mono_ns = mono_ns
        self.rate = Fraction(str(rate))
        self.paused = paused


class ReplayTimeline:
    """Durable event queue plus the clock. Every mutating call runs inside a store transaction."""

    def __init__(self, store: ReplayRunStore) -> None:
        self._store = store

    def cursor_us(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT cursor_us FROM run WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("no replay run exists")
        return int(row[0])

    def schedule(
        self,
        conn: sqlite3.Connection,
        *,
        at_us: int,
        priority: int,
        kind: str,
        payload: Mapping[str, Any],
        max_queued_events: int,
    ) -> int:
        """Queue an event at an absolute market time at or after the cursor; return its sequence."""
        cursor = self.cursor_us(conn)
        if at_us < cursor:
            raise BackdatingError(
                f"event at {at_us} is before the canonical cursor {cursor}; commands cannot be backdated"
            )
        return self._store.enqueue(
            conn,
            at_us=at_us,
            priority=priority,
            kind=kind,
            payload=payload,
            max_queued_events=max_queued_events,
        )

    def advance_to(
        self, conn: sqlite3.Connection, *, target_us: int, end_us: int, handler: EventHandler, max_journal_bytes: int
    ) -> int:
        """Process every due event up to ``min(target, end)``, exclusive of ``end``; return the count.

        Each processed event is journaled and its effect is committed in the caller's transaction,
        together with the cursor. A handler that raises rolls the whole advance back.
        """
        bound = min(target_us, end_us)
        # The target is inclusive (events exactly at it are due); the end is exclusive.
        limit = min(target_us, end_us - 1)
        processed = 0
        while (event := self._store.next_due(conn, limit)) is not None:
            if event.at_us > self.cursor_us(conn):
                self._store.update_run(conn, cursor_us=event.at_us)
            handler(event, conn)
            self._store.set_queue_status(conn, event.seq, "processed")
            self._store.append_journal(
                conn,
                kind="scheduled",
                market_time_us=event.at_us,
                priority=event.priority,
                payload={"kind": event.kind, "payload": event.payload},
                max_journal_bytes=max_journal_bytes,
            )
            processed += 1
        if bound > self.cursor_us(conn):
            self._store.update_run(conn, cursor_us=bound)
        return processed
