"""Terminal progress for research backtests."""

from __future__ import annotations

import time
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

    def replay(self, timestamp: pd.Timestamp, done: int, total: int) -> None:
        """Report tick replay inside the candle opening at ``timestamp``."""

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

    def replay(self, timestamp: pd.Timestamp, done: int, total: int) -> None:
        if self._ticks is None:
            return
        if timestamp != self._replayed:
            self._replayed = timestamp
            self._replays += 1
        self._progress.update(
            self._ticks, completed=done, total=total, status=f"{timestamp:%Y-%m-%d %H:%M} ticks", visible=True
        )

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
