"""Pure, vectorized cursors for overlapping gateway polling windows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np


@dataclass(frozen=True)
class TickCursor:
    last_msc: int | None = None
    seen_at_last_msc: int = 0


@dataclass(frozen=True)
class BarState:
    forming_time: int | None = None
    forming_values: tuple[float, ...] | None = None


def _slice(columns: Mapping[str, np.ndarray], start: int, stop: int | None = None) -> dict[str, np.ndarray]:
    return {name: values[start:stop] for name, values in columns.items()}


def new_ticks(cursor: TickCursor, columns: Mapping[str, np.ndarray]) -> tuple[Mapping[str, np.ndarray], TickCursor]:
    times = columns["time_msc"]
    if len(times) == 0:
        return _slice(columns, 0, 0), cursor
    if cursor.last_msc is None:
        start = 0
    else:
        boundary = int(np.searchsorted(times, cursor.last_msc, side="left"))
        after = int(np.searchsorted(times, cursor.last_msc, side="right"))
        start = boundary + min(cursor.seen_at_last_msc, after - boundary)
    fresh = _slice(columns, start)
    if len(fresh["time_msc"]) == 0:
        return fresh, cursor
    last = int(fresh["time_msc"][-1])
    if cursor.last_msc == last:
        seen = cursor.seen_at_last_msc + int(np.count_nonzero(fresh["time_msc"] == last))
    else:
        seen = int(np.count_nonzero(fresh["time_msc"] == last))
    return fresh, TickCursor(last, seen)


def _bar_values(columns: Mapping[str, np.ndarray], index: int) -> tuple[float, ...]:
    return tuple(
        float(columns[name][index]) for name in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")
    )


def bar_transitions(
    state: BarState, columns: Mapping[str, np.ndarray]
) -> tuple[Mapping[str, np.ndarray] | None, Mapping[str, np.ndarray] | None, BarState]:
    if len(columns["time"]) == 0:
        return None, None, state
    index = len(columns["time"]) - 1
    forming_time = int(columns["time"][index])
    values = _bar_values(columns, index)
    new_state = BarState(forming_time, values)
    forming = _slice(columns, index, index + 1)
    if state.forming_time is None:
        return None, forming, new_state
    if forming_time > state.forming_time:
        completed_index = max(0, index - 1)
        return _slice(columns, completed_index, completed_index + 1), forming, new_state
    if forming_time == state.forming_time and values != state.forming_values:
        return None, forming, new_state
    return None, None, state
