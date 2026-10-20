"""Terminal progress for research backtests and tick imports."""

from __future__ import annotations

import time
from datetime import date, datetime
from types import TracebackType

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


class BacktestProgress:
    """Receives backtest progress and shows nothing; the base for a visible display."""

    def __enter__(self) -> BacktestProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def start(self, total_bars: int) -> None:
        """Announce how many bars the run decides on."""

    def bar(self, done: int, timestamp: pd.Timestamp, position: str = "") -> None:
        """Report that ``done`` bars are decided, the last one opening at ``timestamp``."""

    def replay(self, timestamp: pd.Timestamp, done: int) -> None:
        """Report that ``done`` ticks of the candle opening at ``timestamp`` have been decided on."""

    def finish(self, trades: int) -> None:
        """Report the completed run."""


class RichBacktestProgress(BacktestProgress):
    """A live progress bar on the terminal: bars decided, position, tick replays, timing."""

    def __init__(self, label: str, console: Console) -> None:
        self._label = label
        self._console = console
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[status]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self._bars: TaskID | None = None
        self._ticks: TaskID | None = None
        self._replayed: pd.Timestamp | None = None
        self._replays = 0
        self._started = time.perf_counter()

    def __enter__(self) -> RichBacktestProgress:
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._progress.stop()

    def start(self, total_bars: int) -> None:
        self._started = time.perf_counter()
        self._bars = self._progress.add_task(self._label, total=total_bars, status="")
        self._ticks = self._progress.add_task("  tick replay", total=None, status="", visible=False)

    def bar(self, done: int, timestamp: pd.Timestamp, position: str = "") -> None:
        if self._bars is None or self._ticks is None:
            return
        parts = [f"{timestamp:%Y-%m-%d %H:%M}"]
        if position:
            parts.append(position)
        if self._replays:
            parts.append(f"{_count(self._replays, 'candle')} replayed")
        self._progress.update(self._bars, completed=done, status=" · ".join(parts))
        self._progress.update(self._ticks, visible=False)

    def replay(self, timestamp: pd.Timestamp, done: int) -> None:
        if self._ticks is None:
            return
        if timestamp != self._replayed:
            self._replayed = timestamp
            self._replays += 1
        self._progress.update(self._ticks, completed=done, status=f"{timestamp:%Y-%m-%d %H:%M} ticks", visible=True)

    def finish(self, trades: int) -> None:
        if self._bars is None or self._ticks is None:
            return
        self._progress.update(self._ticks, visible=False)
        self._progress.stop()
        bars = int(self._progress.tasks[self._bars].total or 0)
        seconds = time.perf_counter() - self._started
        self._console.print(
            f"[bold]{self._label}[/bold]: {_count(bars, 'bar')}, {_count(trades, 'trade')}, "
            f"{_count(self._replays, 'candle')} replayed from ticks in {seconds:.1f}s"
        )


def backtest_progress(enabled: bool | None, label: str, *, console: Console | None = None) -> BacktestProgress:
    """A visible display when ``enabled``; by default only when stderr is a terminal."""
    if enabled is False:
        return BacktestProgress()
    target = console if console is not None else Console(stderr=True)
    if enabled is None and not target.is_terminal:
        return BacktestProgress()
    return RichBacktestProgress(label, target)


class TickImportProgress:
    """Daily download progress; Rich keeps elapsed time live during blocking HTTP calls."""

    def __init__(self, label: str, console: Console, *, enabled: bool) -> None:
        self._console = console
        self._enabled = enabled
        self._label = f"{label} ticks"
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[status]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            disable=not enabled,
            # Keep enough samples to estimate ETA even when chunks take minutes.
            speed_estimate_period=1200.0,
        )
        self._task: TaskID | None = None
        self._started = time.perf_counter()
        self._rows = 0

    def __enter__(self) -> TickImportProgress:
        self._progress.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None and self._task is not None:
            self._progress.update(self._task, status="failed; import incomplete")
        self._progress.stop()

    def start(self, total: int) -> None:
        self._started = time.perf_counter()
        self._task = self._progress.add_task(self._label, total=total, status="waiting for MT5")

    def waiting(self, start: datetime, end: datetime) -> None:
        if self._task is not None:
            self._progress.update(
                self._task,
                status=f"{self._rows:,} ticks · {start:%Y-%m-%d %H:%M}–{end:%H:%M} · waiting for MT5",
            )

    def advance(self, done: int, rows: int) -> None:
        self._rows = rows
        if self._task is not None:
            self._progress.update(self._task, completed=done, status=f"{rows:,} ticks received")

    def processing(self) -> None:
        if self._task is not None:
            self._progress.update(self._task, status=f"{self._rows:,} ticks · building DataFrame")

    def finish(self, rows: int) -> None:
        if self._task is not None:
            self._progress.update(self._task, status="complete")
        self._progress.stop()
        if self._enabled:
            self._console.print(f"{self._label}: {rows:,} ticks imported in {time.perf_counter() - self._started:.1f}s")


def tick_import_progress(enabled: bool | None, label: str, *, console: Console | None = None) -> TickImportProgress:
    """Show elapsed time and a chunk-based ETA; default to visibility only on a terminal."""
    target = console if console is not None else Console(stderr=True)
    visible = target.is_terminal if enabled is None else enabled
    return TickImportProgress(label, target, enabled=visible)


class TickSyncProgress:
    """Per-session progress for a tick store sync; one step per weekday considered."""

    def __init__(self, label: str, console: Console, *, enabled: bool) -> None:
        self._console = console
        self._enabled = enabled
        self._label = f"{label} sync"
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[interval]}"),
            TextColumn("{task.fields[throughput]}"),
            TextColumn("{task.fields[session]}"),
            TextColumn("{task.fields[status]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            disable=not enabled,
            speed_estimate_period=1200.0,
        )
        self._task: TaskID | None = None
        self._started = time.perf_counter()
        self._rows = 0

    def __enter__(self) -> TickSyncProgress:
        self._progress.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None and self._task is not None:
            self._progress.update(self._task, status="failed; sync incomplete")
        self._progress.stop()

    def start(self, total: int) -> None:
        self._started = time.perf_counter()
        self._task = self._progress.add_task(
            self._label, total=total, interval="", throughput="", session="", status="preparing tick history"
        )

    def waiting(self, start: date, end: date) -> None:
        if self._task is not None:
            self._progress.update(self._task, interval=f"{start}–{end}", status="downloading / synchronizing MT5")

    def received(self, rows: int) -> None:
        self._rows = rows
        elapsed = max(time.perf_counter() - self._started, 0.001)
        if self._task is not None:
            self._progress.update(
                self._task,
                throughput=f"{rows:,} ticks · {rows / elapsed:,.0f}/s",
                status="downloading / writing sessions",
            )

    def advance(self, done: int, day: date, outcome: str) -> None:
        if self._task is not None:
            self._progress.update(self._task, completed=done, session=f"{day.isoformat()} {outcome}")

    def finish(self) -> None:
        if self._task is not None:
            self._progress.update(self._task, status="complete")
        self._progress.stop()
        if self._enabled and self._task is not None:
            total = int(self._progress.tasks[self._task].total or 0)
            self._console.print(
                f"{self._label}: {total} weekday(s) considered in {time.perf_counter() - self._started:.1f}s"
            )


def tick_sync_progress(enabled: bool | None, label: str, *, console: Console | None = None) -> TickSyncProgress:
    """Show per-day sync progress and ETA; default to visibility only on a terminal."""
    target = console if console is not None else Console(stderr=True)
    visible = target.is_terminal if enabled is None else enabled
    return TickSyncProgress(label, target, enabled=visible)
