"""An M5 strategy and its M1-decision version trade identically on the same ticks (Q-140)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest, closed_bars
from tests.research.test_closed_bars import DAY_ONE, SYMBOL, _write_dense_session, TickStore

TRADE_FIELDS = [
    "side",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "quantity",
    "pnl",
    "exit_reason",
    "entry_reason",
]


class BarDirection(ResearchStrategy):
    """Buy after a bar closes up, close after one closes down. Decides on every bar of its frame."""

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["up"] = frame["close"] > frame["open"]
        out["down"] = frame["close"] < frame["open"]
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy() if not positions and frame["up"].iloc[-1] else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.close() if positions and frame["down"].iloc[-1] else None


class BarDirectionOnM1(ResearchStrategy):
    """The same rule on M1 bars: it reads the last closed M5 bar and decides only when an M5 bar closes."""

    decision_timeframe = "M1"

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        bars = closed_bars(frame, "M5")
        out["up"] = bars["close"] > bars["open"]
        out["down"] = bars["close"] < bars["open"]
        return out

    @staticmethod
    def _closes_an_m5_bar(frame: pd.DataFrame) -> bool:
        return (frame.index[-1] + pd.Timedelta(minutes=1)).minute % 5 == 0

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not self._closes_an_m5_bar(frame):
            return None
        return TradeOrder.buy() if not positions and frame["up"].iloc[-1] else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not self._closes_an_m5_bar(frame):
            return None
        return TradeOrder.close() if positions and frame["down"].iloc[-1] else None


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "ticks"
    _write_dense_session(root, DAY_ONE, seed=3, minutes=120)
    return TickStore(SYMBOL, root=root)


def _trades(result) -> pd.DataFrame:
    return result.trades[TRADE_FIELDS].reset_index(drop=True)


def test_the_m1_decision_version_trades_exactly_like_the_m5_strategy(store):
    m5 = store.bars("M5", start=DAY_ONE.isoformat())
    m1 = store.bars("M1", start=DAY_ONE.isoformat())

    # Excursion and post-exit fields count bars, which differ between the two frames; TRADE_FIELDS skips them.
    on_m5 = backtest(m5, strategy=BarDirection(), symbol=SYMBOL, excursions=False)
    on_m1 = backtest(m1, strategy=BarDirectionOnM1(), symbol=SYMBOL, excursions=False, check_causality=True)

    assert len(on_m5.trades) > 0
    pd.testing.assert_frame_equal(_trades(on_m1), _trades(on_m5))
