"""Progress reports for a running backtest.

``BacktestProgressReporter`` owns the status a backtest publishes while it runs. The job layer
calls ``phase`` at each step boundary; the candle engine's intrabar replay calls ``replay`` for
every candle it is about to read. Reports go through the ``publish`` callable, which the job
layer points at the existing progress transport. Reporting is observational: it never changes
what a run computes, and a reporter built without ``publish`` only tracks state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal, Optional

BacktestPhase = Literal["queued", "loading_data", "preparing", "simulating", "saving"]

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

    @classmethod
    def silent(cls) -> "BacktestProgressReporter":
        """A reporter that tracks nothing visible: callers that run without a job."""
        return cls("", started_at=_utc_now())

    def phase(self, phase: BacktestPhase, message: str, *, bars_total: Optional[int] = None) -> None:
        """Enter ``phase``; always published. ``progress`` is null until a replay reports one."""
        self._phase = phase
        self._message = message
        self._progress = None
        if bars_total is not None:
            self._bars_total = bars_total
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
        """Report that the simulation is about to read the history of candle ``bar``.

        ``bar`` is the candle's position among ``bars_total`` bars, so ``progress`` is
        ``bar / bars_total``. The report is throttled; the state is updated either way.
        """
        self._bars_total = bars_total
        share = bar / bars_total if bars_total else 0.0
        self._progress = max(self._progress or 0.0, min(1.0, share))
        self._simulated_until = candle_time
        self._candles_replayed = replayed
        self._candles_substituted = substituted
        self._message = f"Replaying ticks · {candle_time.date().isoformat()}"
        now = self._clock()
        if self._last_published is not None and now - self._last_published < REPLAY_REPORT_INTERVAL_SECONDS:
            return
        self._emit(now)

    def _emit(self, now: Optional[float] = None) -> None:
        if self._publish is None:
            return
        self._last_published = self._clock() if now is None else now
        self._publish(self._payload())

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
        }
