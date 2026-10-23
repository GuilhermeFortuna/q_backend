"""TickData reads a candle's or a range's ticks, never past its cutoff, and loads a session only when asked (Q-126)."""

from __future__ import annotations

import pickle
from datetime import date

import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import TickData as ExportedTickData
from q_backend.research.errors import NoMarketDataError
from q_backend.research.tick_data import TickData, TickSource
from q_backend.research.tick_store import TickStore
from tests.research.test_stop_target_backtest import DAY, FLAT_0, SYMBOL, _write_session

NEXT_DAY = date(2026, 10, 6)
AFTER_NEXT_DAY = date(2026, 10, 7)
SESSION = FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0), ("10:15:00", 97.0)]
COLUMNS = ["bid", "ask", "last", "volume", "flags"]


def _moment(day: date, clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day.isoformat()} {clock}", tz=BRASILIA_TZ)


def _bars(store: TickStore, first: date, last: date) -> pd.DataFrame:
    return store.bars("M10", start=first.isoformat(), end=f"{last.isoformat()} 23:59:59")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


@pytest.fixture
def reads(monkeypatch):
    """Count the session files a run reads from the store."""
    calls: list[date] = []
    original = TickStore._session_columns

    def counting(self, day):
        calls.append(day)
        return original(self, day)

    monkeypatch.setattr(TickStore, "_session_columns", counting)
    return calls


def test_the_package_exports_tick_data():
    assert ExportedTickData is TickData


def test_bar_returns_the_stored_rows_of_its_candle_including_a_sessions_last_bar(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    last_bar = TickData(TickSource(store), index, current=1)

    assert last_bar.cutoff == _moment(NEXT_DAY, "00:00")
    current = last_bar.bar()
    assert current.index.tolist() == [_moment(DAY, c) for c in ("10:10:00", "10:11:00", "10:12:00", "10:15:00")]
    assert current["last"].tolist() == [100.0, 96.0, 94.0, 97.0]
    assert last_bar.bar(index[0])["last"].tolist() == [100.0, 100.0]


def test_a_timestamp_outside_the_frame_raises(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    ticks = TickData(TickSource(store), index, current=1)

    with pytest.raises(ValueError, match="not a bar of the backtest frame"):
        ticks.bar(_moment(DAY, "09:50:00"))


def test_a_bar_that_opens_at_or_after_the_cutoff_raises(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    ticks = TickData(TickSource(store), index, current=0)

    assert ticks.cutoff == index[1]
    with pytest.raises(ValueError, match="opens at or after the cutoff"):
        ticks.bar(index[1])


def test_between_is_clipped_to_the_cutoff_and_raises_from_at_or_after_it(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    ticks = TickData(TickSource(store), index, current=0)

    clipped = ticks.between(_moment(DAY, "10:00:00"), _moment(DAY, "10:30:00"))
    assert clipped["last"].tolist() == [100.0, 100.0]
    with pytest.raises(ValueError, match="at or after the cutoff"):
        ticks.between(_moment(DAY, "10:12:00"), _moment(DAY, "10:30:00"))


def test_between_returns_an_empty_frame_with_the_store_columns_when_no_ticks_fall_in_range(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    ticks = TickData(TickSource(store), index, current=1)

    empty = ticks.between(_moment(DAY, "10:01:00"), _moment(DAY, "10:04:00"))
    assert empty.empty
    assert list(empty.columns) == COLUMNS
    assert empty.index.tz is not None


def test_hooks_that_never_ask_for_ticks_read_no_session(root, reads):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index

    TickData(TickSource(store), index, current=1)

    assert reads == []


def test_a_bar_by_bar_run_reads_each_session_once(root, reads):
    store = _write_session(root, SESSION)
    _write_session(root, SESSION, day=NEXT_DAY)
    _write_session(root, SESSION, day=AFTER_NEXT_DAY)
    index = _bars(store, DAY, AFTER_NEXT_DAY).index
    source = TickSource(store)

    for position in range(len(index)):
        TickData(source, index, current=position).bar()

    assert reads == [DAY, NEXT_DAY, AFTER_NEXT_DAY]


def test_a_session_that_is_not_stored_raises_naming_the_session_and_sync(root):
    store = _write_session(root, SESSION)
    index = pd.DatetimeIndex([_moment(DAY, "10:00:00"), _moment(NEXT_DAY, "10:00:00")], name="time")
    ticks = TickData(TickSource(store), index, current=1)

    with pytest.raises(NoMarketDataError, match=NEXT_DAY.isoformat()) as exc_info:
        ticks.bar()
    assert "TickStore.sync" in str(exc_info.value)


def test_between_skips_days_without_bars_in_the_frame(root):
    store = _write_session(root, SESSION)
    _write_session(root, SESSION, day=AFTER_NEXT_DAY)
    index = pd.DatetimeIndex([_moment(DAY, "10:00:00"), _moment(AFTER_NEXT_DAY, "10:00:00")], name="time")
    ticks = TickData(TickSource(store), index, current=1)

    ranged = ticks.between(_moment(DAY, "00:00:00"), _moment(AFTER_NEXT_DAY, "23:00:00"))
    assert len(ranged) == 2 * len(SESSION)


def test_between_across_sessions_returns_their_ticks_in_order(root):
    store = _write_session(root, SESSION)
    _write_session(root, [("10:00:00", 50.0), ("10:05:00", 51.0)], day=NEXT_DAY)
    index = pd.DatetimeIndex([_moment(DAY, "10:00:00"), _moment(NEXT_DAY, "10:00:00")], name="time")
    ticks = TickData(TickSource(store), index, current=1)

    across = ticks.between(_moment(DAY, "10:10:00"), _moment(NEXT_DAY, "23:00:00"))
    assert across["last"].tolist() == [100.0, 96.0, 94.0, 97.0, 50.0, 51.0]


def test_returned_frames_are_private_copies(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    ticks = TickData(TickSource(store), index, current=1)

    first = ticks.bar()
    first.iloc[0, first.columns.get_loc("last")] = -1.0
    first.attrs["note"] = "changed"

    again = ticks.bar()
    assert again["last"].iloc[0] == 100.0
    assert again.attrs == {}


def test_a_pickled_tick_source_drops_the_cached_session(root):
    store = _write_session(root, SESSION)
    index = _bars(store, DAY, DAY).index
    source = TickSource(store)
    TickData(source, index, current=1).bar()

    restored = pickle.loads(pickle.dumps(source))

    assert restored._columns is None
    assert restored.session(DAY)["time_msc"].size == len(SESSION)
    assert restored.store.symbol == SYMBOL
