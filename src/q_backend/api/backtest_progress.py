"""Progress reports for a running backtest.

``BacktestProgressReporter`` owns the status a backtest publishes while it runs. The job layer
calls ``phase`` at each step boundary; the candle engine's intrabar replay calls ``replay`` for
every candle it has finished replaying. Reports go through the ``publish`` callable, which the job
layer points at the existing progress transport. Reporting is observational: it never changes
what a run computes, and a reporter built without ``publish`` only tracks state.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from q_backend.api.schemas.backtest import BacktestActivityKind

logger = logging.getLogger(__name__)

BacktestPhase = Literal["queued", "loading_data", "preparing", "simulating", "measuring", "saving"]

REPLAY_REPORT_INTERVAL_SECONDS = 0.5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BacktestProgressReporter:
    """Build, throttle and publish the progress payload of one backtest run.

    Phase changes are always published. Replay reports are published at most once per
    ``REPLAY_REPORT_INTERVAL_SECONDS`` as measured by ``clock``; a throttled report updates the
    state that the next publish carries. ``progress`` never decreases within a run.
    """

    def __init__(
        self,
        run_id: str,
        *,
        started_at: datetime,
        publish: Optional[Callable[[dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._run_id = run_id
        self._started_at = started_at
        self._publish = publish
        self._clock = clock
        self._wall_clock = wall_clock
        self._last_published: Optional[float] = None
        self._phase: Optional[BacktestPhase] = None
        self._message: Optional[str] = None
        self._progress: Optional[float] = None
        self._bars_total: Optional[int] = None
        self._simulated_until: Optional[datetime] = None
        self._candles_replayed = 0
        self._candles_substituted = 0
        self._activity: Optional[dict[str, Any]] = None
        self._phase_history: list[dict[str, Any]] = [
            {"phase": "queued", "started_at": started_at.isoformat(), "finished_at": None}
        ]

    @classmethod
    def silent(cls) -> "BacktestProgressReporter":
        """A reporter that tracks nothing visible: callers that run without a job."""
        return cls("", started_at=_utc_now())

    def phase(self, phase: BacktestPhase, message: str, *, bars_total: Optional[int] = None) -> None:
        """Enter ``phase``; always published. ``progress`` is null until a replay reports one."""
        self._phase = phase
        entered_at = self._wall_clock().isoformat()
        if self._phase_history[-1]["phase"] != phase:
            self._phase_history[-1]["finished_at"] = entered_at
            self._phase_history.append({"phase": phase, "started_at": entered_at, "finished_at": None})
        self._activity = None
        if phase in {"preparing", "simulating", "measuring", "saving"}:
            self._activity = {
                "kind": "saving" if phase == "saving" else "computing",
                "started_at": entered_at,
                "range_start": None,
                "range_end": None,
            }
        self._message = message
        self._progress = None
        if bars_total is not None:
            self._bars_total = bars_total
        self._emit()

    @contextmanager
    def read(self, kind: BacktestActivityKind, start: datetime, end: datetime):
        """Publish a pending read immediately; only a successful return resumes computation."""
        self._activity = {
            "kind": kind,
            "started_at": self._wall_clock().isoformat(),
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
        }
        self._emit()
        yield
        self._activity = {
            "kind": "computing",
            "started_at": self._wall_clock().isoformat(),
            "range_start": None,
            "range_end": None,
        }
        self._emit()

    def replay(
        self,
        bar: int,
        bars_total: int,
        candle_time: datetime,
        *,
        replayed: int,
        substituted: int,
    ) -> None:
        """Report a completed replay of candle ``bar`` (zero-based).

        ``bar`` is the candle's position among ``bars_total`` bars, so ``progress`` is
        ``(bar + 1) / bars_total``. This is historical coverage, not a remaining-time estimate.
        """
        self._bars_total = bars_total
        share = (bar + 1) / bars_total if bars_total else 0.0
        self._progress = max(self._progress or 0.0, min(1.0, share))
        if self._simulated_until is None or candle_time > self._simulated_until:
            self._simulated_until = candle_time
        self._candles_replayed = replayed
        self._candles_substituted = substituted
        self._message = f"Replaying ticks · {candle_time.date().isoformat()}"
        now = self._clock()
        if self._last_published is not None and now - self._last_published < REPLAY_REPORT_INTERVAL_SECONDS:
            return
        self._emit(now)

    def measured(self, count: int, total: int) -> None:
        """Report excursion measurement progress by completed trade."""
        self._progress = count / total if total else 1.0
        self._message = f"Measuring trade excursions · {count}/{total} trades"
        now = self._clock()
        if (
            self._last_published is None
            or now - self._last_published >= REPLAY_REPORT_INTERVAL_SECONDS
            or count == total
        ):
            self._emit(now)

    def _emit(self, now: Optional[float] = None) -> None:
        if self._publish is None:
            return
        self._last_published = self._clock() if now is None else now
        try:
            self._publish(self._payload())
        except Exception:  # noqa: BLE001 - reporting must not fail the backtest
            logger.debug("Progress publication unavailable for %s", self._run_id, exc_info=True)

    def _payload(self) -> dict[str, Any]:
        return {
            "run_id": self._run_id,
            # The transport's status stays "running" while the run is live; the phase carries the step.
            "status": "running",
            "error": None,
            "phase": self._phase,
            "progress": self._progress,
            "message": self._message,
            "started_at": self._started_at.isoformat(),
            "updated_at": self._wall_clock().isoformat(),
            "simulated_until": None if self._simulated_until is None else self._simulated_until.isoformat(),
            "bars_total": self._bars_total,
            "candles_replayed": self._candles_replayed,
            "candles_substituted": self._candles_substituted,
            "activity": None if self._activity is None else dict(self._activity),
            "phase_history": [dict(entry) for entry in self._phase_history],
        }
