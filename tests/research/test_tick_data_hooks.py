"""Strategy hooks declare a ``ticks`` parameter and read ticks up to their decision moment (Q-126)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TickData, TradeOrder, backtest
from q_backend.research.errors import NoMarketDataError
from q_backend.research.tick_store import TickStore
from tests.research.test_stop_target_backtest import (
    DAY,
    FLAT_0,
    SYMBOL,
    _m10_frame,
    _stamp,
    _write_session,
)

NEXT_DAY = date(2026, 10, 6)
AFTER_NEXT_DAY = date(2026, 10, 7)
SESSION = FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0), ("10:15:00", 97.0)]
BREAKOUT = [("10:20:00", 101.0), ("10:21:00", 104.0), ("10:21:00", 104.5), ("10:22:00", 105.5), ("10:23:00", 103.0)]
BREAKOUT_SESSION = FLAT_0 + [("10:10:00", 100.0)] + BREAKOUT


def _moment(day: date, clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day.isoformat()} {clock}", tz=BRASILIA_TZ)


def _bars(store: TickStore, first: date, last: date) -> pd.DataFrame:
    return store.bars("M10", start=first.isoformat(), end=f"{last.isoformat()} 23:59:59")


def _backtest(frame, strategy, store=None, **kwargs):
    return backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store, **kwargs)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


@pytest.fixture
def reads(monkeypatch):
    """Count the session files Python reads from the store during a run."""
    calls: list[date] = []
    original = TickStore._session_columns

    def counting(self, day):
        calls.append(day)
        return original(self, day)

    monkeypatch.setattr(TickStore, "_session_columns", counting)
    return calls


class Optional(ResearchStrategy):
    """Declares an optional ``ticks`` on compute_indicators and records what it receives."""

    def __init__(self) -> None:
        self.received: list[object] = []

    def compute_indicators(self, frame, ticks=None):
        self.received.append(ticks)
        return frame

    def entry_strategy(self, frame, positions=()):
        return None


class Unannotated(ResearchStrategy):
    """Takes no ``ticks``; ``**kwargs`` alone must not receive one."""

    def compute_indicators(self, frame, **kwargs):
        assert kwargs == {}
        return frame

    def entry_strategy(self, frame, positions=(), **kwargs):
        assert kwargs == {}
        return None


class RequiresInIndicators(ResearchStrategy):
    def compute_indicators(self, frame, *, ticks):
        raise AssertionError("compute_indicators must not run without a store")

    def entry_strategy(self, frame, positions=()):
        return None


class RequiresInEntry(ResearchStrategy):
    def entry_strategy(self, frame, positions=(), *, ticks):
        return None


class RequiresInExit(ResearchStrategy):
    def entry_strategy(self, frame, positions=()):
        return None

    def exit_strategy(self, frame, positions=(), *, phase="bar", ticks):
        return None


class Probe(ResearchStrategy):
    """Enters on the first bar and records what each tick-aware hook sees."""

    def __init__(self) -> None:
        self.entry_views: list[tuple] = []
        self.screen_views: list[tuple] = []
        self.tick_views: list[tuple] = []

    def entry_strategy(self, frame, positions=(), ticks=None):
        if ticks is not None:
            self.entry_views.append((frame.index[-1], ticks.cutoff, ticks.bar()["last"].tolist()))
        return TradeOrder.buy() if len(frame) == 1 else None

    def exit_strategy(self, frame, positions=(), *, phase="bar", ticks=None):
        if phase == "screen" and positions:
            # A screen that asks for a close hands the candle to the tick phase.
            try:
                ticks.bar()
                raised = False
            except ValueError as exc:
                raised = "opens at or after the cutoff" in str(exc)
            self.screen_views.append((ticks.cutoff, raised))
            return TradeOrder.close()
        if phase == "tick":
            self.tick_views.append((ticks.cutoff, ticks.bar()["last"].tolist()))
        return None


class NeverReads(ResearchStrategy):
    """Declares ``ticks`` on every hook and never calls it."""

    def compute_indicators(self, frame, ticks=None):
        return frame

    def entry_strategy(self, frame, positions=(), ticks=None):
        return TradeOrder.buy() if len(frame) == 1 else None

    def exit_strategy(self, frame, positions=(), *, phase="bar", ticks=None):
        return None


class EveryBarVolume(ResearchStrategy):
    """Reads the current candle's ticks on every bar, and enters once a candle has more than three trades."""

    def __init__(self) -> None:
        self.entered = False

    def entry_strategy(self, frame, positions=(), ticks=None):
        busy = ticks.bar()["last"].size > 3
        if self.entered or not busy:
            return None
        self.entered = True
        return TradeOrder.buy()


class PreviousSessionTicks(ResearchStrategy):
    """A causal per-bar column: the count of the previous session's ticks, known at the session's open."""

    def compute_indicators(self, frame, ticks=None):
        out = frame.copy()
        out["previous_ticks"] = [
            self._previous_count(ticks, moment.tz_convert(BRASILIA_TZ).normalize()) for moment in frame.index
        ]
        return out

    @staticmethod
    def _previous_count(ticks, day_start: pd.Timestamp) -> int:
        return len(ticks.between(day_start - pd.Timedelta(days=1), day_start))

    def entry_strategy(self, frame, positions=()):
        return None


class NextSessionTicks(ResearchStrategy):
    """Reads the next session's ticks whenever they are visible: non-causal, so the check must catch it."""

    def compute_indicators(self, frame, ticks=None):
        out = frame.copy()
        values = []
        for moment in frame.index:
            day_start = moment.tz_convert(BRASILIA_TZ).normalize()
            next_start = day_start + pd.Timedelta(days=1)
            count = 0
            if next_start < ticks.cutoff:
                count = len(ticks.between(next_start, next_start + pd.Timedelta(days=1)))
            values.append(count)
        out["next_ticks"] = values
        return out

    def entry_strategy(self, frame, positions=()):
        return None


class DaySession(ResearchStrategy):
    """Day-trade strategy for the worker parity test: buys the first bar with a busy candle."""

    def entry_strategy(self, frame, positions=(), ticks=None):
        if positions:
            return None
        return TradeOrder.buy() if ticks.bar()["last"].size > 1 else None


def test_tick_data_is_exported_from_the_research_package():
    from q_backend.research import TickData as Exported

    assert Exported is TickData


def test_a_hook_without_ticks_runs_with_or_without_a_store(root):
    store = _write_session(root, SESSION)
    frame = _m10_frame(store)

    _backtest(frame, Unannotated())
    _backtest(frame, Unannotated(), store)


def test_an_optional_ticks_parameter_receives_none_without_a_store_and_a_tick_data_with_one(root):
    store = _write_session(root, SESSION)
    frame = _m10_frame(store)

    without = Optional()
    _backtest(frame, without)
    with_store = Optional()
    _backtest(frame, with_store, store)

    assert without.received == [None]
    assert isinstance(with_store.received[0], TickData)


@pytest.mark.parametrize(
    ("strategy", "hook"),
    [
        (RequiresInIndicators, "compute_indicators"),
        (RequiresInEntry, "entry_strategy"),
        (RequiresInExit, "exit_strategy"),
    ],
)
def test_a_required_ticks_parameter_without_a_store_raises_before_any_hook_runs(root, strategy, hook):
    store = _write_session(root, SESSION)

    with pytest.raises(ValueError, match=f"{strategy.__name__}.{hook}") as exc_info:
        _backtest(_m10_frame(store), strategy())
    assert "ticks=" in str(exc_info.value)


def test_a_required_ticks_hook_runs_when_a_store_is_given(root):
    store = _write_session(root, SESSION)

    result = _backtest(_m10_frame(store), RequiresInEntry(), store)
    assert result.trades.empty


def test_an_entry_hook_sees_the_bars_ticks_up_to_its_bar_end_only(root):
    store = _write_session(root, SESSION)
    frame = _m10_frame(store)
    probe = Probe()

    _backtest(frame, probe, store)

    first_bar, last_bar = probe.entry_views
    assert first_bar == (_stamp("10:00"), _stamp("10:10"), [100.0, 100.0])
    assert last_bar[0] == _stamp("10:10")
    assert last_bar[1] == _moment(NEXT_DAY, "00:00:00")
    assert last_bar[2] == [100.0, 96.0, 94.0, 97.0]


def test_a_screen_hook_cannot_read_the_candle_it_is_screening(root):
    store = _write_session(root, BREAKOUT_SESSION)
    probe = Probe()

    _backtest(_m10_frame(store), probe, store)

    assert probe.screen_views
    assert all(raised for _cutoff, raised in probe.screen_views)
    assert _stamp("10:20") in [cutoff for cutoff, _raised in probe.screen_views]


def test_a_tick_hook_reads_up_to_the_decided_trade_even_with_a_same_millisecond_row(root):
    store = _write_session(root, BREAKOUT_SESSION)
    probe = Probe()

    _backtest(_m10_frame(store), probe, store)

    breakout = [view for view in probe.tick_views if view[0] >= _stamp("10:20")]
    assert [cutoff for cutoff, _ in breakout] == [
        _stamp("10:20"),
        _stamp("10:21"),
        _stamp("10:21"),
        _stamp("10:22"),
        _stamp("10:23"),
    ]
    assert [last for _, last in breakout] == [
        [101.0],
        [101.0, 104.0],
        [101.0, 104.0, 104.5],
        [101.0, 104.0, 104.5, 105.5],
        [101.0, 104.0, 104.5, 105.5, 103.0],
    ]


def test_a_strategy_that_never_calls_ticks_reads_no_session(root, reads):
    store = _write_session(root, BREAKOUT_SESSION)

    _backtest(_m10_frame(store), NeverReads(), store)

    assert reads == []


def test_a_bar_by_bar_strategy_reads_each_session_once(root, reads):
    store = _write_session(root, SESSION)
    _write_session(root, SESSION, day=NEXT_DAY)
    _write_session(root, SESSION, day=AFTER_NEXT_DAY)
    frame = _bars(store, DAY, AFTER_NEXT_DAY)

    result = _backtest(frame, EveryBarVolume(), store)

    assert reads == [DAY, NEXT_DAY, AFTER_NEXT_DAY]
    assert len(result.data) == len(frame)


def test_a_compute_indicators_column_built_from_earlier_sessions_passes_the_causality_check(root):
    store = _write_session(root, SESSION)
    _write_session(root, FLAT_0 + [("10:10:00", 50.0)], day=NEXT_DAY)
    frame = _bars(store, DAY, NEXT_DAY)

    result = _backtest(frame, PreviousSessionTicks(), store, check_causality=True)

    previous = result.data["previous_ticks"].tolist()
    assert previous == [0, 0, len(SESSION), len(SESSION)]


def test_a_compute_indicators_column_reading_the_next_session_fails_the_causality_check(root):
    store = _write_session(root, SESSION)
    _write_session(root, FLAT_0 + [("10:10:00", 50.0)], day=NEXT_DAY)
    frame = _bars(store, DAY, NEXT_DAY)

    with pytest.raises(ValueError, match="not causal: column 'next_ticks' at bar") as exc_info:
        _backtest(frame, NextSessionTicks(), store, check_causality=True)
    assert str(_stamp("10:00")) in str(exc_info.value)


def test_a_hook_reading_a_session_that_is_not_stored_fails_naming_it(root):
    store = _write_session(root, SESSION)
    frame = _m10_frame(store).copy()
    extra = frame.iloc[[1]].copy()
    extra.index = pd.DatetimeIndex([_moment(NEXT_DAY, "10:00:00")], name="time")
    extra.loc[:, ["open", "high", "low", "close"]] = [100.0, 100.0, 80.0, 85.0]
    frame = pd.concat([frame, extra])

    class ReadsEveryBar(ResearchStrategy):
        def entry_strategy(self, frame, positions=(), ticks=None):
            ticks.bar()
            return None

    with pytest.raises(RuntimeError, match=NEXT_DAY.isoformat()) as exc_info:
        _backtest(frame, ReadsEveryBar(), store)
    assert isinstance(exc_info.value.__cause__, NoMarketDataError)


def test_a_day_trade_strategy_reading_ticks_gives_the_same_result_with_two_workers(root):
    store = _write_session(root, SESSION)
    _write_session(root, SESSION, day=NEXT_DAY)
    _write_session(root, SESSION, day=AFTER_NEXT_DAY)
    frame = _bars(store, DAY, AFTER_NEXT_DAY)

    single = _backtest(frame, DaySession(), store, day_trade=True, workers=1)
    pair = _backtest(frame, DaySession(), store, day_trade=True, workers=2)

    assert not single.trades.empty
    # Trade ids are random per run; every other column must match exactly.
    pd.testing.assert_frame_equal(single.trades.drop(columns="trade_id"), pair.trades.drop(columns="trade_id"))
    pd.testing.assert_frame_equal(single.equity, pair.equity)
