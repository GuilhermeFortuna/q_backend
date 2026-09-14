from pathlib import Path

import numpy as np

from q_backend.streaming.market.cursor import BarState, TickCursor, bar_transitions, new_ticks


def _ticks(start: int, stop: int):
    source = np.load(Path(__file__).parents[1] / "fixtures/market/synthetic_b3_ticks_session.npz")
    return {name: source[name][start:stop] for name in source.files}


def test_overlapping_fixture_windows_publish_every_tick_once_in_order():
    cursor = TickCursor()
    out = []
    for start in (0, 250, 500, 750):
        fresh, cursor = new_ticks(cursor, _ticks(start, start + 300))
        out.append(fresh["time_msc"])
    combined = np.concatenate(out)
    assert np.array_equal(combined, _ticks(0, 1050)["time_msc"])


def test_same_millisecond_boundary_keeps_remaining_ticks():
    columns = {"time_msc": np.array([1, 2, 2, 2, 3]), "bid": np.arange(5)}
    first, cursor = new_ticks(TickCursor(), {name: values[:3] for name, values in columns.items()})
    second, _ = new_ticks(cursor, columns)
    assert first["time_msc"].tolist() + second["time_msc"].tolist() == [1, 2, 2, 2, 3]


def test_empty_window_keeps_cursor():
    cursor = TickCursor(42, 2)
    fresh, next_cursor = new_ticks(cursor, {"time_msc": np.array([], dtype=np.int64)})
    assert len(fresh["time_msc"]) == 0
    assert next_cursor == cursor


def _bars(times, closes):
    count = len(times)
    return {
        "time": np.asarray(times, dtype=np.int64),
        "open": np.ones(count),
        "high": np.ones(count),
        "low": np.ones(count),
        "close": np.asarray(closes),
        "tick_volume": np.ones(count, dtype=np.int64),
        "spread": np.zeros(count, dtype=np.int64),
        "real_volume": np.zeros(count, dtype=np.int64),
    }


def test_bar_transitions_emit_only_changed_forming_and_completed_before_roll():
    _, forming, state = bar_transitions(BarState(), _bars([10], [1.0]))
    assert forming is not None
    assert bar_transitions(state, _bars([10], [1.0]))[:2] == (None, None)
    _, forming, state = bar_transitions(state, _bars([10], [2.0]))
    assert forming is not None
    completed, forming, _ = bar_transitions(state, _bars([10, 20], [2.0, 3.0]))
    assert completed is not None and completed["time"].tolist() == [10]
    assert forming is not None and forming["time"].tolist() == [20]
