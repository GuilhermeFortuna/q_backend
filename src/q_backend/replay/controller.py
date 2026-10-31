"""Replay run lifecycle: create, pause, resume, speed, step, stop and status.

Commands carry a UUID and the revision the client observed. A repeated UUID returns the recorded
result without applying anything again; a stale revision is refused and recorded. Every accepted
command, refusal and processed event is committed with the cursor in one transaction. The
in-memory clock changes only after that commit succeeds, so a failed write leaves the run as it
was.

Lifecycle: ``prepared -> running <-> paused -> completed | stopped``, with ``failed`` after a
handler error. A restart recovers a running run as paused; the simulator never jumps forward by
its downtime.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from q_backend.replay.config import MAX_RATE, MIN_RATE, ReplayRunConfig
from q_backend.replay.journal import ReplayRunStore, RunRecord, StoredCommand
from q_backend.replay.timeline import EventHandler, ReplayClock, ReplayTimeline

PREPARED: Final = "prepared"
PAUSED: Final = "paused"
RUNNING: Final = "running"
COMPLETED: Final = "completed"
STOPPED: Final = "stopped"
FAILED: Final = "failed"
_TERMINAL: Final = frozenset({COMPLETED, STOPPED, FAILED})

PAUSE: Final = "pause"
RESUME: Final = "resume"
SPEED: Final = "speed"
STEP: Final = "step"
STOP: Final = "stop"
ACTIONS: Final = frozenset({PAUSE, RESUME, SPEED, STEP, STOP})

_CURSOR_PRIORITY: Final = 0


class ReplayNotFoundError(LookupError):
    """No run has been created on this simulator instance."""


def _noop_handler(event: object, conn: sqlite3.Connection) -> None:
    return None


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    generation: str
    state: str
    revision: int
    rate: float
    canonical_cursor_us: int
    visible_cursor_us: int
    queue_depth: int
    config_hash: str
    dataset_checksums: tuple[str, ...]
    last_error: str | None


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    accepted: bool
    reason: str | None
    revision: int
    state: str

    @classmethod
    def from_stored(cls, stored: StoredCommand) -> CommandResult:
        return cls(
            command_id=stored.command_id,
            accepted=stored.accepted,
            reason=stored.reason,
            revision=stored.revision,
            state=stored.state,
        )


class ReplayController:
    """Single owner of one simulator instance's run. Not thread-safe; callers serialize access."""

    def __init__(
        self,
        store: ReplayRunStore,
        *,
        handler: EventHandler = _noop_handler,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        on_finish: Callable[[sqlite3.Connection, int], None] | None = None,
    ) -> None:
        self._store = store
        self._timeline = ReplayTimeline(store)
        self._handler = handler
        self._on_finish = on_finish
        self._mono = mono_ns
        self._clock: ReplayClock | None = None
        run = store.load_run()
        if run is not None:
            self._recover(run)

    # lifecycle ----------------------------------------------------------------

    def create(
        self,
        config: ReplayRunConfig,
        *,
        run_id: str,
        generation: str,
        dataset_checksums: tuple[str, ...],
        setup: Callable[[sqlite3.Connection], None] | None = None,
    ) -> RunStatus:
        """Record a new run in ``prepared`` state, positioned at the interval start.

        ``setup`` runs in the same transaction, so components that schedule their first events
        (for example the market feed) either all exist with the run or none do.
        """
        start_us = config.start_us
        with self._store.transaction() as conn:
            self._store.create_run(
                conn,
                run_id=run_id,
                generation=generation,
                state=PREPARED,
                rate=str(config.rate),
                cursor_us=start_us,
                end_us=config.end_us,
                config_hash=config.config_hash(),
                config_json=config.canonical_json(),
                dataset_checksums=dataset_checksums,
                max_queued_events=config.limits.max_queued_events,
                max_journal_bytes=config.limits.max_journal_bytes,
            )
            self._store.append_journal(
                conn,
                kind="cursor",
                market_time_us=start_us,
                priority=_CURSOR_PRIORITY,
                payload={"event": "created", "config_hash": config.config_hash()},
                max_journal_bytes=config.limits.max_journal_bytes,
            )
            if setup is not None:
                setup(conn)
        self._clock = ReplayClock.start(rate=config.rate, market_us=start_us, mono_ns=self._mono(), paused=True)
        return self.status()

    def command(
        self,
        *,
        command_id: str,
        expected_revision: int,
        action: str,
        rate: float | None = None,
        market_ms: int | None = None,
    ) -> CommandResult:
        """Apply one control command, or return the recorded result for a repeated command_id."""
        if action not in ACTIONS:
            raise ValueError(f"unknown replay action: {action}")
        prior = self._store.get_command(command_id)
        if prior is not None:
            return CommandResult.from_stored(prior)
        run = self._require_run()
        clock = self._require_clock()
        mono = self._mono()

        reason = self._refusal(run, expected_revision, action, rate, market_ms)
        if reason is not None:
            return self._record_refusal(command_id, action, expected_revision, reason, run)

        # Commands are processed at "now": a running clock first delivers every event that is
        # already due, so a command never trades against an earlier unseen market move.
        now_us = min(clock.now_us(mono), run.end_us) if run.state == RUNNING else run.cursor_us
        if action == STEP:
            assert market_ms is not None  # validated by _refusal
            now_us = run.cursor_us + market_ms * 1000
        advances = run.state == RUNNING or action == STEP
        new_rate = rate if action == SPEED and rate is not None else float(run.rate)

        with self._store.transaction() as conn:
            cursor_after = run.cursor_us
            if advances:
                self._timeline.advance_to(
                    conn,
                    target_us=min(now_us, run.end_us),
                    end_us=run.end_us,
                    handler=self._handler,
                    max_journal_bytes=run.max_journal_bytes,
                )
                cursor_after = self._timeline.cursor_us(conn)
            if action == STOP:
                if self._on_finish is not None:
                    self._on_finish(conn, cursor_after)
                cancelled = self._store.cancel_pending(conn)
                state_after = STOPPED
                if cancelled:
                    self._store.append_journal(
                        conn,
                        kind="cursor",
                        market_time_us=cursor_after,
                        priority=_CURSOR_PRIORITY,
                        payload={"event": "cancelled_pending", "count": cancelled},
                        max_journal_bytes=run.max_journal_bytes,
                    )
            elif cursor_after >= run.end_us:
                state_after = COMPLETED
                if self._on_finish is not None:
                    self._on_finish(conn, run.end_us)
            else:
                state_after = _state_after(action, run.state)
            revision_after = run.revision + 1
            self._store.update_run(
                conn,
                state=state_after,
                revision=revision_after,
                rate=str(new_rate),
                cursor_us=cursor_after,
            )
            result = StoredCommand(
                command_id=command_id,
                action=action,
                expected_revision=expected_revision,
                accepted=True,
                reason=None,
                revision=revision_after,
                state=state_after,
            )
            self._store.record_command(conn, result)
            self._store.append_journal(
                conn,
                kind="cursor",
                market_time_us=cursor_after,
                priority=_CURSOR_PRIORITY,
                payload={
                    "event": "command",
                    "action": action,
                    "command_id": command_id,
                    "state": state_after,
                    "rate": new_rate,
                },
                max_journal_bytes=run.max_journal_bytes,
            )

        clock.reanchor(
            market_us=cursor_after,
            mono_ns=mono,
            rate=new_rate,
            paused=state_after != RUNNING,
        )
        return CommandResult.from_stored(result)

    def tick(self) -> int:
        """Advance a running run to its real-time market position; return events processed."""
        run = self._require_run()
        if run.state != RUNNING:
            return 0
        clock = self._require_clock()
        mono = self._mono()
        target = min(clock.now_us(mono), run.end_us)
        try:
            with self._store.transaction() as conn:
                processed = self._timeline.advance_to(
                    conn,
                    target_us=target,
                    end_us=run.end_us,
                    handler=self._handler,
                    max_journal_bytes=run.max_journal_bytes,
                )
                cursor = self._timeline.cursor_us(conn)
                state_after = COMPLETED if cursor >= run.end_us else RUNNING
                if state_after == COMPLETED:
                    if self._on_finish is not None:
                        self._on_finish(conn, run.end_us)
                    self._store.update_run(conn, state=COMPLETED, revision=run.revision + 1, cursor_us=cursor)
                    self._store.append_journal(
                        conn,
                        kind="cursor",
                        market_time_us=cursor,
                        priority=_CURSOR_PRIORITY,
                        payload={"event": "completed"},
                        max_journal_bytes=run.max_journal_bytes,
                    )
        except Exception as exc:
            self._fail(str(exc))
            raise
        clock.reanchor(
            market_us=cursor,
            mono_ns=mono,
            rate=float(run.rate),
            paused=state_after != RUNNING,
        )
        return processed

    # reads ------------------------------------------------------------------

    def status(self) -> RunStatus:
        """Report cursors without processing events; ``tick`` is what delivers due market moves."""
        run = self._require_run()
        clock = self._require_clock()
        canonical = run.cursor_us
        if run.state == RUNNING:
            canonical = min(clock.now_us(self._mono()), run.end_us)
        market_data_ms = json.loads(run.config_json)["latency"]["market_data_ms"]
        visible = max(0, canonical - market_data_ms * 1000)
        return RunStatus(
            run_id=run.run_id,
            generation=run.generation,
            state=run.state,
            revision=run.revision,
            rate=float(run.rate),
            canonical_cursor_us=canonical,
            visible_cursor_us=visible,
            queue_depth=self._pending(),
            config_hash=run.config_hash,
            dataset_checksums=run.dataset_checksums,
            last_error=run.last_error,
        )

    def journal(self) -> list[tuple[int, str, int, str, str]]:
        return self._store.journal_entries()

    # internals --------------------------------------------------------------

    def _require_run(self) -> RunRecord:
        run = self._store.load_run()
        if run is None:
            raise ReplayNotFoundError("no replay run exists on this simulator")
        return run

    def _require_clock(self) -> ReplayClock:
        if self._clock is None:
            raise RuntimeError("replay clock is not initialized")
        return self._clock

    def _pending(self) -> int:
        with self._store.transaction() as conn:
            return self._store.pending_count(conn)

    def _refusal(
        self,
        run: RunRecord,
        expected_revision: int,
        action: str,
        rate: float | None,
        market_ms: int | None,
    ) -> str | None:
        if action == SPEED:
            if rate is None:
                raise ValueError("speed requires a rate")
        elif rate is not None:
            raise ValueError(f"{action} does not take a rate")
        if action == STEP:
            if market_ms is None or market_ms < 1:
                raise ValueError("step requires a positive market_ms")
        elif market_ms is not None:
            raise ValueError(f"{action} does not take market_ms")
        if expected_revision != run.revision:
            return "stale_revision"
        if run.state in _TERMINAL:
            return "run_not_active"
        if action == SPEED and not (math.isfinite(rate) and MIN_RATE <= rate <= MAX_RATE):  # type: ignore[arg-type]
            return "invalid_rate"
        if action == STEP and run.state != PAUSED:
            return "step_requires_pause"
        if action == RESUME and run.state not in (PREPARED, PAUSED):
            return "invalid_transition"
        if action == PAUSE and run.state not in (PREPARED, RUNNING):
            return "invalid_transition"
        return None

    def _record_refusal(
        self, command_id: str, action: str, expected_revision: int, reason: str, run: RunRecord
    ) -> CommandResult:
        stored = StoredCommand(
            command_id=command_id,
            action=action,
            expected_revision=expected_revision,
            accepted=False,
            reason=reason,
            revision=run.revision,
            state=run.state,
        )
        with self._store.transaction() as conn:
            self._store.record_command(conn, stored)
            self._store.append_journal(
                conn,
                kind="cursor",
                market_time_us=run.cursor_us,
                priority=_CURSOR_PRIORITY,
                payload={
                    "event": "command_refused",
                    "action": action,
                    "command_id": command_id,
                    "reason": reason,
                },
                max_journal_bytes=run.max_journal_bytes,
            )
        return CommandResult.from_stored(stored)

    def _fail(self, message: str) -> None:
        with self._store.transaction() as conn:
            self._store.update_run(conn, state=FAILED, last_error=message)
        if self._clock is not None:
            self._clock.paused = True

    def _recover(self, run: RunRecord) -> None:
        if run.state == RUNNING:
            with self._store.transaction() as conn:
                self._store.update_run(conn, state=PAUSED)
                self._store.append_journal(
                    conn,
                    kind="cursor",
                    market_time_us=run.cursor_us,
                    priority=_CURSOR_PRIORITY,
                    payload={"event": "recovered_paused"},
                    max_journal_bytes=run.max_journal_bytes,
                )
        self._clock = ReplayClock.start(
            rate=float(run.rate), market_us=run.cursor_us, mono_ns=self._mono(), paused=True
        )


def _state_after(action: str, state: str) -> str:
    """Lifecycle transition for an accepted, non-terminal command."""
    if action == PAUSE:
        return PAUSED
    if action == RESUME:
        return RUNNING
    return state  # speed and step keep the state (step is only accepted while paused)
