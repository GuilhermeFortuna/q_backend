"""Forming candles for tick-phase exit decisions, and worker-count resolution for research backtests."""

from __future__ import annotations

import os
from typing import Literal

import numpy as np
import pandas as pd

_FORMING_COLUMNS = ("open", "high", "low", "close", "tick_volume")

# Memory budgeted per worker when sizing "auto": each one imports the research stack and the
# strategy's module, then holds a copy of the prepared history.
_AUTO_BYTES_PER_WORKER = 1 << 30


def _available_memory() -> int | None:
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def resolve_workers(workers: int | Literal["auto"]) -> int:
    """Validate ``workers`` and size ``"auto"`` from the usable CPUs and the free memory."""
    if workers == "auto":
        if hasattr(os, "sched_getaffinity"):
            cpus = len(os.sched_getaffinity(0))
        else:
            cpus = os.cpu_count() or 1
        memory = _available_memory()
        if memory is not None:
            cpus = min(cpus, memory // _AUTO_BYTES_PER_WORKER)
        return max(1, cpus)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError(f"workers must be a positive integer or 'auto', got {workers!r}")
    return workers


def forming_compatible(frame: pd.DataFrame) -> pd.DataFrame:
    """Give boolean and integer columns a dtype that can hold a forming row's missing values.

    Integers become float64, as a concatenated forming row already made them. Booleans become
    object columns of ``numpy.bool_`` values, so ``~`` stays logical. Nullable dtypes would turn
    every missing float in a row into ``pd.NA``, which breaks ordinary ``NaN`` comparisons in hooks.
    """
    for loc, dtype in enumerate(frame.dtypes):
        if dtype.kind in "iu":
            frame.isetitem(loc, frame.iloc[:, loc].to_numpy(dtype=np.float64))
        elif dtype.kind == "b":
            values = np.empty(len(frame), dtype=object)
            values[:] = list(frame.iloc[:, loc].to_numpy())
            frame.isetitem(loc, values)
    return frame


class FormingRow:
    """Shows a forming candle as the last row of the prepared history, one tick decision at a time.

    The row takes the candle's observed open, high, low, close and tick count; every other column
    is missing, so no indicator of the completed candle reaches a tick decision. The row is
    restored to its prepared values when the next bar starts, when a bar-level hook reads it, and
    when the run ends. Frames handed out are positional slices of the history, so no history is
    copied per tick.
    """

    def __init__(self, history: pd.DataFrame) -> None:
        self._history = history
        self._market = [(name, history.columns.get_loc(name)) for name in _FORMING_COLUMNS if name in history.columns]
        self._saved: list[object] = []
        self._position: int | None = None

    def frame(self, position: int, candle: tuple[float, float, float, float], tick_count: int) -> pd.DataFrame:
        """Prepared history through ``position``, with its last row showing the forming candle."""
        if self._position != position:
            self.restore()
            self._claim(position)
        history = self._history
        values = {"open": candle[0], "high": candle[1], "low": candle[2], "close": candle[3], "tick_volume": tick_count}
        for name, loc in self._market:
            history.iloc[position, loc] = values[name]
        view = history.iloc[: position + 1]
        view.attrs = {}
        return view

    def restore(self) -> None:
        """Put the forming row back to its prepared values; a no-op when none is showing."""
        if self._position is None:
            return
        for loc, value in enumerate(self._saved):
            self._history.iloc[self._position, loc] = value
        self._position = None
        self._saved = []

    def _claim(self, position: int) -> None:
        history = self._history
        self._saved = history.iloc[position].tolist()
        market = {loc for _name, loc in self._market}
        for loc in range(history.shape[1]):
            if loc not in market:
                history.iloc[position, loc] = np.nan
        self._position = position
