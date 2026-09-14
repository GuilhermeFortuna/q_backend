"""Pure, vectorized cursors for overlapping gateway polling windows."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # The last forming bar as published, used when a roll's window no longer holds it.
    forming: Mapping[str, np.ndarray] | None = field(default=None, compare=False)


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
    forming = _slice(columns, index, index + 1)
    new_state = BarState(forming_time, values, forming)
    if state.forming_time is None:
        return None, forming, new_state
    if forming_time > state.forming_time:
        return _completed_since(state, columns, index), forming, new_state
    if forming_time == state.forming_time and values != state.forming_values:
        return None, forming, new_state
    return None, None, state


def _completed_since(state: BarState, columns: Mapping[str, np.ndarray], index: int) -> Mapping[str, np.ndarray]:
    """Every bar from the previous forming bar up to, not including, the new one.

    Normally the window starts at the previous forming bar, which then carries its
    final values. If the window does not reach back to it, the last published
    forming values stand in for it, so no completed bar is skipped and the new
    forming bar is never reported as completed.
    """
    first = int(np.searchsorted(columns["time"], state.forming_time, side="left"))
    completed = _slice(columns, first, index)
    if first < index and int(columns["time"][first]) == state.forming_time:
        return completed
    if state.forming is None:
        return completed
    return {name: np.concatenate([state.forming[name], completed[name]]) for name in completed}
