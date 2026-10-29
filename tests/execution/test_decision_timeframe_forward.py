"""Forward decisions of an M1-declaring strategy version match its backtest on the same bars (Q-140)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from q_backend.backtesting.models import Signal
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.parity import reference_research_decisions_by_close, signals_equal
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TradeOrder, closed_bars

SYMBOL = "WIN$N"


class BarDirectionOnM1(ResearchStrategy):
    """Buys after an M5 bar closes up and closes after one closes down; decides only at M5 closes."""

    decision_timeframe = "M1"

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        bars = closed_bars(frame, "M5")
        out["up"] = bars["close"] > bars["open"]
        out["down"] = bars["close"] < bars["open"]
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not _closes_m5_bar(frame) or positions:
            return None
        return TradeOrder.buy() if frame["up"].iloc[-1] else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not _closes_m5_bar(frame) or not positions:
            return None
        return TradeOrder.close() if frame["down"].iloc[-1] else None


def _closes_m5_bar(frame: pd.DataFrame) -> bool:
    return (frame.index[-1] + pd.Timedelta(minutes=1)).minute % 5 == 0


def _synthetic_m1_bars(n_days: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(20260301)
    frames = []
    price = 100.0
    for day_offset in range(n_days):
        day = (dt.date(2026, 3, 2) + dt.timedelta(days=day_offset)).isoformat()
        times = pd.date_range(f"{day} 09:00", f"{day} 15:59", freq="1min", tz=BRASILIA_TZ)
        close = price + np.cumsum(rng.normal(0.0, 0.3, size=len(times)))
        price = close[-1]
        open_ = np.concatenate([[close[0]], close[:-1]])
        high = np.maximum(open_, close) + np.abs(rng.normal(0.05, 0.05, size=len(times)))
        low = np.minimum(open_, close) - np.abs(rng.normal(0.05, 0.05, size=len(times)))
        frames.append(
            pd.DataFrame(
                {
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "tick_volume": rng.integers(10, 100, size=len(times)),
                    "spread": np.nan,
                    "real_volume": np.nan,
                },
                index=times,
            )
        )
    return pd.concat(frames)


def test_forward_decisions_of_an_m1_version_match_its_backtest_on_the_same_bars():
    data = _synthetic_m1_bars()
    identity = StrategyIdentity(
        strategy_name="BarDirectionOnM1",
        strategy_version=1,
        compiled_config={
            "source_kind": "strategy_version",
            "strategy_params": {},
            "symbol": SYMBOL,
            "timeframe": "M1",
            "day_trade": True,
            "day_trade_start_time": "09:00",
            "day_trade_end_time": "16:00",
            "day_trade_close_time": "17:00",
        },
        config_hash="m1-parity",
        symbol=SYMBOL,
        timeframe="M1",
    )
    reference = reference_research_decisions_by_close(
        BarDirectionOnM1(),
        data,
        symbol=SYMBOL,
        timeframe="M1",
        day_trade=True,
        day_trade_start_time="09:00",
        day_trade_end_time="16:00",
        day_trade_close_time="17:00",
    )
    evaluator = ResearchEvaluator(
        deployment_id="m1-parity",
        identity=identity,
        strategy_instance=BarDirectionOnM1(),
        window_bound=2000,
    )
    evaluator.seed_window(data.iloc[:30])

    entries_seen = 0
    for position in range(30, len(data)):
        result = evaluator.ingest_completed_bar(data.iloc[position : position + 1])
        assert result is not None

        reference_close, reference_exits, reference_entries = reference[position]
        assert result.bar_close_time == reference_close
        forward_exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
        forward_entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
        assert signals_equal(reference_exits, forward_exits), f"exit mismatch at {reference_close}"
        assert signals_equal(reference_entries, forward_entries), f"entry mismatch at {reference_close}"
        entries_seen += len(forward_entries)

    assert entries_seen > 0
