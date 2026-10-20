"""The opt-in check that indicators read only the bars before them (Q-121)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from tests.research.test_stop_target_backtest import SYMBOL, _write_session

SESSION_ONE = [("10:00:00", 100.0), ("10:05:00", 101.0), ("10:10:00", 102.0), ("10:15:00", 103.0)]
SESSION_TWO = [("10:00:00", 110.0), ("10:05:00", 111.0), ("10:10:00", 112.0), ("10:15:00", 113.0)]


class Causal(ResearchStrategy):
    def __init__(self) -> None:
        self.computed = 0

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.computed += 1
        out = frame.copy()
        out["ma"] = out["close"].rolling(2, min_periods=1).mean()
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy() if len(frame) == 1 else None


class LooksAhead(Causal):
    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.computed += 1
        out = frame.copy()
        out["next_close"] = out["close"].shift(-1)
        return out


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "ticks"
    _write_session(root, SESSION_ONE)
    # A second session on the next day gives the check a session end to compare at.
    return _write_session(root, SESSION_TWO, day=date(2026, 10, 6))


def _frame(store):
    return store.bars("M10", start="2026-10-05")


def test_a_lookahead_indicator_is_reported_by_column_and_bar(store):
    frame = _frame(store)

    with pytest.raises(ValueError, match=r"column 'next_close'") as exc_info:
        backtest(frame, strategy=LooksAhead(), symbol=SYMBOL, check_causality=True)

    # The last closed bar of the first session is the first to read the next session's close.
    assert "2026-10-05 10:10" in str(exc_info.value)


def test_a_causal_indicator_passes_the_check(store):
    result = backtest(_frame(store), strategy=Causal(), symbol=SYMBOL, check_causality=True)

    assert len(result.trades) == 1


def test_the_check_is_off_by_default_and_then_indicators_run_once(store):
    strategy = Causal()
    backtest(_frame(store), strategy=strategy, symbol=SYMBOL)

    assert strategy.computed == 1


def test_the_check_recomputes_indicators_on_prefixes_ending_at_session_ends(store):
    strategy = Causal()
    backtest(_frame(store), strategy=strategy, symbol=SYMBOL, check_causality=True)

    # One run over the full history, plus one prefix ending at the end of the first session.
    assert strategy.computed == 2


def test_the_check_applies_to_custom_strategies_only(store):
    with pytest.raises(ValueError, match="ResearchStrategy"):
        backtest(_frame(store), strategy="MACrossover", symbol=SYMBOL, check_causality=True)
