import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from q_backend.backtesting.costs import TransactionCostConfig, side_cost
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.optimization.backtest_runner import BacktestRunConfig, DefaultBacktestRunner


class DummyStrategy(TradingStrategy):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.symbol = "DUMMY"

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        return write_signal_columns(
            df,
            entry_long=(df["close"] == 100.0),
            entry_short=pd.Series(False, index=df.index, dtype=bool),
            exit_long=(df["close"] == 110.0),
            exit_short=False,
            strategy_name=type(self).__name__,
        )

    def get_chart_indicators(self):
        return []


@pytest.fixture
def swing_trade_data():
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [
        base,
        base + timedelta(hours=1),
        base + timedelta(days=1),
        base + timedelta(days=1, hours=1),
        base + timedelta(days=2),
        base + timedelta(days=2, hours=1),
    ]
    closes = [100.0, 105.0, 105.0, 105.0, 110.0, 115.0]
    opens = [99.0, 101.0, 104.0, 106.0, 108.0, 112.0]
    return pd.DataFrame({"open": opens, "close": closes}, index=times)


def test_side_cost_per_contract():
    config = TransactionCostConfig(cost_per_contract=5.0)
    assert side_cost(config, price=100.0, quantity=2.0, point_value=1.0) == 10.0


def test_side_cost_bps():
    config = TransactionCostConfig(cost_bps=100.0)
    assert side_cost(config, price=100.0, quantity=1.0, point_value=1.0) == pytest.approx(1.0)


def test_round_trip_cost_per_contract_reduces_pnl(swing_trade_data):
    costs = TransactionCostConfig(cost_per_contract=5.0)
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=2.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000, costs=costs)

    registry = engine.run(swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL)

    trade = registry.get_closed_trades()[0]
    gross_pnl = (112.0 - 101.0) * 2.0
    assert trade.commission == pytest.approx(20.0)
    assert trade.pnl == pytest.approx(gross_pnl - 20.0)


def test_cost_bps_uses_respective_fill_prices(swing_trade_data):
    costs = TransactionCostConfig(cost_bps=100.0)
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000, costs=costs)

    registry = engine.run(swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL)

    trade = registry.get_closed_trades()[0]
    entry_cost = side_cost(costs, 101.0, 1.0, 1.0)
    exit_cost = side_cost(costs, 112.0, 1.0, 1.0)
    assert trade.commission == pytest.approx(entry_cost + exit_cost)
    assert trade.pnl == pytest.approx((112.0 - 101.0) - (entry_cost + exit_cost))


def test_zero_costs_match_baseline(swing_trade_data):
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)

    baseline = BacktestEngine(strategy, sizer, initial_capital=1000).run(
        swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL
    )
    with_none = BacktestEngine(strategy, sizer, initial_capital=1000, costs=None).run(
        swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL
    )
    with_zero = BacktestEngine(
        strategy,
        sizer,
        initial_capital=1000,
        costs=TransactionCostConfig(),
    ).run(swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL)

    for registry in (with_none, with_zero):
        trade = registry.get_closed_trades()[0]
        baseline_trade = baseline.get_closed_trades()[0]
        assert trade.entry_price == baseline_trade.entry_price
        assert trade.exit_price == baseline_trade.exit_price
        assert trade.pnl == baseline_trade.pnl
        assert trade.commission == 0.0


def test_day_trade_parallel_mode_pays_costs(swing_trade_data):
    costs = TransactionCostConfig(cost_per_contract=5.0)
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=2.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000, costs=costs)

    registry = engine.run(swing_trade_data, parallel_mode=ParallelMode.DAY_TRADE)

    trade = registry.get_closed_trades()[0]
    gross_pnl = (105.0 - 101.0) * 2.0
    assert trade.commission == pytest.approx(20.0)
    assert trade.pnl == pytest.approx(gross_pnl - 20.0)


def test_total_commission_metric(swing_trade_data):
    costs = TransactionCostConfig(cost_per_contract=5.0)
    engine = BacktestEngine(
        DummyStrategy(),
        FixedQuantitySizer(quantity=2.0),
        initial_capital=1000,
        costs=costs,
    )
    registry = engine.run(swing_trade_data, parallel_mode=ParallelMode.SEQUENTIAL)

    metrics = registry.get_performance_metrics()
    assert metrics["total_commission"] == pytest.approx(20.0)


def test_optimizer_backtest_runner_passes_costs():
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        {"open": [99.0, 101.0], "close": [100.0, 105.0]},
        index=[base, base + timedelta(hours=1)],
    )
    costs = TransactionCostConfig(cost_per_contract=3.0)
    config = BacktestRunConfig(
        symbol="DUMMY",
        timeframe="H1",
        start=base,
        end=base + timedelta(hours=2),
        initial_capital=1000.0,
        point_value=1.0,
        strategy="MACrossover",
        strategy_params={"short_period": 2, "long_period": 4, "threshold": 1.0},
        position_sizing=None,
        costs=costs,
    )
    runner = DefaultBacktestRunner(data_provider=lambda _cfg: df)

    captured: list[TransactionCostConfig | None] = []

    original_init = BacktestEngine.__init__

    def spy_init(self, strategy, sizer, *args, **kwargs):
        captured.append(kwargs.get("costs"))
        return original_init(self, strategy, sizer, *args, **kwargs)

    with patch.object(BacktestEngine, "__init__", spy_init):
        runner.run(config)

    assert captured == [costs]


def test_empty_registry_total_commission():
    metrics = TradeRegistry().get_performance_metrics()
    assert metrics["total_commission"] == 0.0
