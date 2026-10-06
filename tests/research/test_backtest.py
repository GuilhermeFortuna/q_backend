"""Tests for research backtest() facade and BacktestResult."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import (
    BacktestResult,
    ResearchStrategy,
    TradeOrder,
    backtest,
)
from q_backend.research.adapter import ResearchStrategyAdapter
from q_backend.research.results import TRADE_COLUMNS


@pytest.fixture
def ohlcv_5m() -> pd.DataFrame:
    # 20 bars of 5-minute data with realistic price movements
    idx = pd.date_range("2026-09-01 09:00", periods=20, freq="5min", tz=BRASILIA_TZ, name="time")
    # Base pattern: up, down, up
    closes = [
        100.0,
        101.0,
        102.0,
        103.0,
        102.5,  # 0-4
        101.0,
        100.0,
        99.0,
        98.0,
        99.5,  # 5-9
        101.0,
        102.0,
        103.5,
        104.0,
        103.0,  # 10-14
        102.0,
        101.0,
        100.0,
        101.0,
        102.0,  # 15-19
    ]
    opens = [c - 0.5 for c in closes]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    tick_volumes = [100 + i * 5 for i in range(20)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": tick_volumes,
        },
        index=idx,
    )


class CrossoverHelper(ResearchStrategy):
    """Simple crossover for parity testing."""

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame["sma3"] = frame["close"].rolling(3).mean()
        frame["sma5"] = frame["close"].rolling(5).mean()
        return frame

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) < 6:
            return None
        # buy when sma3 > sma5 and previous <=
        curr3 = frame["sma3"].iloc[-1]
        curr5 = frame["sma5"].iloc[-1]
        prev3 = frame["sma3"].iloc[-2]
        prev5 = frame["sma5"].iloc[-2]
        if prev3 <= prev5 and curr3 > curr5:
            return TradeOrder.buy()
        if prev3 >= prev5 and curr3 < curr5:
            return TradeOrder.sell()
        return None

    def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return None


def test_parity_with_direct_engine_calls(ohlcv_5m: pd.DataFrame) -> None:
    # 1. Custom compiled strategy parity
    custom_strat = CrossoverHelper()
    res_custom = backtest(
        ohlcv_5m,
        strategy=custom_strat,
        symbol="PETR4",
        quantity=100,
        point_value=1.0,
        initial_capital=50_000.0,
        force_close_at_end=True,
    )

    # Direct engine run with ResearchStrategyAdapter
    adapter = ResearchStrategyAdapter(CrossoverHelper(), symbol="PETR4")
    engine = BacktestEngine(
        strategy=adapter,
        sizer=FixedQuantitySizer(quantity=100),
        initial_capital=50_000.0,
        point_values={"PETR4": 1.0},
    )
    registry_direct = engine.run(ohlcv_5m, force_close_at_end=True)
    direct_metrics = registry_direct.get_performance_metrics(50_000.0)

    assert res_custom.metrics == direct_metrics
    assert len(res_custom.trades) == len(registry_direct.get_all_trades())
    if len(res_custom.trades) > 0:
        direct_trades = registry_direct.get_all_trades()
        for i, dt in enumerate(direct_trades):
            row = res_custom.trades.iloc[i]
            assert row["entry_price"] == pytest.approx(dt.entry_price)
            assert row["exit_price"] == pytest.approx(dt.exit_price)
            assert row["pnl"] == pytest.approx(dt.pnl)

    # 2. Built-in MACrossover parity
    res_builtin = backtest(
        ohlcv_5m,
        strategy="MACrossover",
        symbol="PETR4",
        strategy_params={"short_period": 3, "long_period": 5},
        quantity=100,
        point_value=1.0,
        initial_capital=50_000.0,
        force_close_at_end=True,
    )

    direct_strat = build_strategy("MACrossover", {"short_period": 3, "long_period": 5}, symbol="PETR4")
    direct_engine_mac = BacktestEngine(
        strategy=direct_strat,
        sizer=FixedQuantitySizer(quantity=100),
        initial_capital=50_000.0,
        point_values={"PETR4": 1.0},
    )
    registry_mac = direct_engine_mac.run(ohlcv_5m, force_close_at_end=True)
    assert res_builtin.metrics == registry_mac.get_performance_metrics(50_000.0)


def test_custom_indicator_hook_called_once(ohlcv_5m: pd.DataFrame) -> None:
    hook_count = 0

    class CountStrategy(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            nonlocal hook_count
            hook_count += 1
            frame = frame.copy()
            frame["my_feature"] = 42.0
            return frame

        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            return None

    res = backtest(
        ohlcv_5m,
        strategy=CountStrategy(),
        symbol="PETR4",
        exit_params={"stop_loss_pct": 0.02},  # configured exit param
    )
    # Ensure compute_indicators was called exactly once across the entire backtest() call
    assert hook_count == 1
    # Check configured exit columns or user columns are in result data
    assert "my_feature" in res.data.columns
    # Ensure internal columns omitted
    assert "q_signal_entry" not in res.data.columns
    assert "bar_index" not in res.data.columns


def test_boundary_cases_and_validation(ohlcv_5m: pd.DataFrame) -> None:
    strat = CrossoverHelper()

    # Naive timestamp index fails
    naive_df = ohlcv_5m.copy()
    naive_df.index = naive_df.index.tz_localize(None)
    with pytest.raises(ValueError, match="DataFrame index has no timezone"):
        backtest(naive_df, strategy=strat, symbol="WIN$")

    # Invalid quantity (0, negative, bool)
    with pytest.raises(ValueError, match="quantity must be a positive integer"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", quantity=0)
    with pytest.raises(ValueError, match="quantity must be a positive integer"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", quantity=True)  # type: ignore[arg-type]

    # Invalid point_value
    with pytest.raises(ValueError, match="point_value must be a finite positive number"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", point_value=-1.0)

    # Invalid initial_capital
    with pytest.raises(ValueError, match="initial_capital must be a finite positive number"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", initial_capital=0.0)

    # Invalid costs
    bad_costs = TransactionCostConfig()
    object.__setattr__(bad_costs, "cost_per_contract", -1.0)
    with pytest.raises(ValueError, match="cost_per_contract must be finite and >= 0"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", costs=bad_costs)
    with pytest.raises(TypeError, match="costs must be TransactionCostConfig or None"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", costs={"cost_per_contract": 1.0})  # type: ignore[arg-type]

    # Invalid day trade session bounds
    with pytest.raises(ValueError, match="Invalid day-trade session order"):
        backtest(
            ohlcv_5m,
            strategy=strat,
            symbol="WIN$",
            day_trade=True,
            day_trade_start_time="17:00",
            day_trade_end_time="16:00",
            day_trade_close_time="17:00",
        )

    # Custom strategy rejects strategy_params
    with pytest.raises(ValueError, match="strategy_params cannot be passed"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", strategy_params={"foo": "bar"})

    # Unknown exit_param
    with pytest.raises(ValueError, match="Unknown exit_param"):
        backtest(ohlcv_5m, strategy=strat, symbol="WIN$", exit_params={"non_existent_rule": 123})

    # Builtin unknown/conflicting params
    with pytest.raises(ValueError, match="Unknown strategy parameter"):
        backtest(ohlcv_5m, strategy="MACrossover", symbol="WIN$", strategy_params={"unknown_p": 1})
    with pytest.raises(ValueError, match="Conflicting parameters"):
        backtest(
            ohlcv_5m,
            strategy="MACrossover",
            symbol="WIN$",
            strategy_params={"stop_loss_pct": 0.05},
            exit_params={"stop_loss_pct": 0.02},
        )


def test_non_brasilia_utc_input_converted(ohlcv_5m: pd.DataFrame) -> None:
    utc_df = ohlcv_5m.copy()
    utc_df.index = utc_df.index.tz_convert("UTC")

    class SimpleBuy(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            if len(frame) == 2:
                return TradeOrder.buy()
            return None

    res = backtest(utc_df, strategy=SimpleBuy(), symbol="WIN$")
    assert str(res.data.index.tz) == "America/Sao_Paulo"
    assert str(res.equity.index.tz) == "America/Sao_Paulo"
    assert str(res.trades["entry_time"].dt.tz) == "America/Sao_Paulo"


def test_empty_input_and_results_schema() -> None:
    empty_df = pd.DataFrame(
        columns=["open", "high", "low", "close", "tick_volume"],
        index=pd.DatetimeIndex([], tz=BRASILIA_TZ, name="time"),
    )

    class Dummy(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            return None

    res = backtest(empty_df, strategy=Dummy(), symbol="WIN$")
    assert res.metrics["total_trades"] == 0
    assert len(res.trades) == 0
    assert list(res.trades.columns) == TRADE_COLUMNS
    assert len(res.equity) == 0
    assert "realized_equity" in res.equity.columns
    assert len(res.data) == 0


def test_realized_equity_equals_initial_capital_plus_closed_pnl(ohlcv_5m: pd.DataFrame) -> None:
    class WinTradeStrat(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            if len(frame) == 2:
                return TradeOrder.buy()
            return None

        def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            if len(frame) == 4:
                return TradeOrder.close()
            return None

    init_cap = 20_000.0
    costs = TransactionCostConfig(cost_per_contract=2.0)
    res = backtest(
        ohlcv_5m,
        strategy=WinTradeStrat(),
        symbol="WIN$",
        quantity=2,
        point_value=0.2,
        initial_capital=init_cap,
        costs=costs,
    )

    assert len(res.trades) == 1
    trade = res.trades.iloc[0]
    assert trade["status"] == "closed"
    assert trade["commission"] == pytest.approx(8.0)  # 2 contracts * 2.0 on entry + 2 * 2.0 on exit

    # Verify final realized equity matches initial capital + total closed PnL
    final_equity = res.equity["realized_equity"].iloc[-1]
    expected_equity = init_cap + res.metrics["total_pnl"]
    assert final_equity == pytest.approx(expected_equity)


def test_open_trade_and_unforced_vs_forced_close(ohlcv_5m: pd.DataFrame) -> None:
    class OpenStrat(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            if len(frame) == 2:
                return TradeOrder.buy()
            return None

    # 1. force_close_at_end=False: trade stays open
    res_open = backtest(ohlcv_5m, strategy=OpenStrat(), symbol="WIN$", force_close_at_end=False)
    assert len(res_open.trades) == 1
    t_open = res_open.trades.iloc[0]
    assert t_open["status"] == "open"
    assert pd.isna(t_open["exit_time"])
    assert pd.isna(t_open["exit_price"])
    assert pd.isna(t_open["pnl"])
    assert res_open.metrics["total_trades"] == 0  # metrics reflect closed trades

    # 2. force_close_at_end=True: trade is closed at the final bar
    res_closed = backtest(ohlcv_5m, strategy=OpenStrat(), symbol="WIN$", force_close_at_end=True)
    assert len(res_closed.trades) == 1
    t_closed = res_closed.trades.iloc[0]
    assert t_closed["status"] == "closed"
    assert not pd.isna(t_closed["exit_time"])
    assert not pd.isna(t_closed["pnl"])
    assert res_closed.metrics["total_trades"] == 1
