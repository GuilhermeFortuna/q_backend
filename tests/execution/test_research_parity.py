"""Parity tests: verify ResearchEvaluator decisions match the reference research backtest."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.models import Signal
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.parity import reference_research_decisions_by_close, signals_equal
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TradeOrder


def _synthetic_bars(n_days: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(20240102)
    frames = []
    base_price = 100.0
    for day_offset in range(n_days):
        day = (dt.date(2024, 1, 2) + dt.timedelta(days=day_offset)).isoformat()
        # 15-minute bars from 09:00 to 17:00 (32 bars)
        times = pd.date_range(f"{day} 09:00", f"{day} 16:45", freq="15min", tz=BRASILIA_TZ)
        steps = rng.normal(0.0, 0.5, size=len(times))
        close = base_price + np.cumsum(steps)
        base_price = close[-1]
        high = close + np.abs(rng.normal(0.2, 0.3, size=len(times)))
        low = close - np.abs(rng.normal(0.2, 0.3, size=len(times)))
        open_ = (high + low) / 2.0
        volume = rng.integers(500, 2000, size=len(times)).astype(float)
        frames.append(
            pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=times)
        )
    return pd.concat(frames)


class ParityMACrossover(ResearchStrategy):
    parameters = {}

    def __init__(self, short_period: int = 4, long_period: int = 12):
        self.short_period = short_period
        self.long_period = long_period

    def compute_indicators(self, frame):
        out = frame.copy()
        out["fast"] = frame["close"].rolling(self.short_period).mean()
        out["slow"] = frame["close"].rolling(self.long_period).mean()
        return out

    def entry_strategy(self, frame, positions=()):
        if pd.isna(frame["slow"].iloc[-1]) or positions:
            return None
        if frame["fast"].iloc[-1] > frame["slow"].iloc[-1] and frame["fast"].iloc[-2] <= frame["slow"].iloc[-2]:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and not pd.isna(frame["slow"].iloc[-1]):
            if frame["fast"].iloc[-1] < frame["slow"].iloc[-1]:
                return TradeOrder.close()
        return None


def _queued_from_result(result) -> tuple[list[Signal], list[Signal]]:
    exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
    entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
    return exits, entries


def test_research_evaluator_parity_with_reference():
    data = _synthetic_bars(n_days=2)
    strategy = ParityMACrossover()
    symbol = "WIN$N"
    timeframe = "M15"
    day_trade_start = "09:30"
    day_trade_end = "16:00"
    day_trade_close = "16:30"

    identity = StrategyIdentity(
        strategy_name="ParityMACrossover",
        strategy_version=1,
        compiled_config={
            "source_kind": "strategy_version",
            "strategy_params": {},
            "symbol": symbol,
            "timeframe": timeframe,
            "day_trade": True,
            "day_trade_start_time": day_trade_start,
            "day_trade_end_time": day_trade_end,
            "day_trade_close_time": day_trade_close,
        },
        config_hash="parity-cfg",
        symbol=symbol,
        timeframe=timeframe,
    )

    reference = reference_research_decisions_by_close(
        strategy,
        data,
        symbol=symbol,
        timeframe=timeframe,
        day_trade=True,
        day_trade_start_time=day_trade_start,
        day_trade_end_time=day_trade_end,
        day_trade_close_time=day_trade_close,
    )

    evaluator = ResearchEvaluator(
        deployment_id="parity-dep",
        identity=identity,
        strategy_instance=strategy,
        window_bound=200,
    )

    evaluator.seed_window(data.iloc[:15])

    for i in range(15, len(data)):
        bar = data.iloc[i : i + 1]
        res = evaluator.ingest_completed_bar(bar)
        assert res is not None

        ref_close, ref_exits, ref_entries = reference[i]
        assert res.bar_close_time == ref_close

        fwd_exits, fwd_entries = _queued_from_result(res)
        assert signals_equal(ref_exits, fwd_exits), f"Exit mismatch at bar {i} ({res.bar_close_time})"
        assert signals_equal(ref_entries, fwd_entries), f"Entry mismatch at bar {i} ({res.bar_close_time})"
