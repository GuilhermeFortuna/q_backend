import numpy as np
import pandas as pd

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.backtesting.position_sizing import build_position_sizer
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays


def _ticks_from_prices(
    prices: np.ndarray,
    spread: float = 0.0,
    msc_per_tick: int = 1000,
    day_offset_msc: int = 0,
) -> TickArrays:
    n = len(prices)
    bid = prices - spread / 2
    ask = prices + spread / 2
    base = 1_700_000_000_000 + day_offset_msc
    time_msc = base + np.arange(n, dtype=np.int64) * msc_per_tick
    return TickArrays(
        time_msc=time_msc,
        bid=bid.astype(np.float64),
        ask=ask.astype(np.float64),
        last=prices.astype(np.float64),
        volume=np.ones(n, dtype=np.float64),
    )


def test_tick_engine_end_to_end():
    prices = np.array([10.0, 11.0, 12.0, 11.0, 10.0])
    ticks = _ticks_from_prices(prices)
    strategy = build_tick_strategy(
        "TickMaBreakout",
        {
            "short_period": 2,
            "long_period": 3,
            "threshold": 0.0,
            "sl_points": 0.0,
            "tp_points": 0.0,
        },
        "TEST",
    )
    sizing = FixedQuantityPositionSizing(quantity=1.0)
    engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing,
        initial_capital=10000.0,
        point_value=1.0,
        symbol="TEST",
    )
    registry = engine.run(ticks, parallel_mode=ParallelMode.SEQUENTIAL)
    assert len(registry.get_closed_trades()) >= 0
    metrics = registry.get_performance_metrics(10000.0)
    assert "total_pnl" in metrics


def test_day_trade_matches_sequential_single_day():
    ticks = _ticks_from_prices(np.array([10.0, 11.0, 12.0, 11.0, 10.0]))

    params = {
        "short_period": 2,
        "long_period": 3,
        "threshold": 0.0,
        "sl_points": 0.0,
        "tp_points": 0.0,
    }
    strategy = build_tick_strategy("TickMaBreakout", params, "TEST")
    sizing = FixedQuantityPositionSizing(quantity=1.0)

    seq_engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing,
        initial_capital=10000.0,
        point_value=1.0,
        symbol="TEST",
    )
    day_engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing,
        initial_capital=10000.0,
        point_value=1.0,
        symbol="TEST",
    )

    seq_registry = seq_engine.run(ticks, parallel_mode=ParallelMode.SEQUENTIAL)
    day_registry = day_engine.run(ticks, parallel_mode=ParallelMode.DAY_TRADE)

    seq_pnl = seq_registry.get_performance_metrics(10000.0)["total_pnl"]
    day_pnl = day_registry.get_performance_metrics(10000.0)["total_pnl"]
    assert seq_pnl == day_pnl
    assert len(seq_registry.get_closed_trades()) == len(day_registry.get_closed_trades())


def test_parity_with_candle_engine_loose():
    """Coarse ticks vs candle engine: same MA logic, no SL/TP, loose tolerance."""
    from q_backend.backtesting.factory import build_strategy

    n = 80
    rng = np.random.default_rng(42)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, size=n))
    open_ = close - rng.normal(0, 0.1, size=n)
    index = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    ohlcv = pd.DataFrame(
        {
            "open": open_,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": rng.integers(100, 200, size=n),
        },
        index=index,
    )

    candle_strategy = build_strategy(
        "MACrossover",
        {"short_period": 5, "long_period": 10, "threshold": 0.0},
        "TEST",
    )
    candle_engine = BacktestEngine(
        candle_strategy,
        build_position_sizer(FixedQuantityPositionSizing(quantity=1.0)),
        initial_capital=100000.0,
        point_values={"TEST": 1.0},
    )
    candle_registry = candle_engine.run(ohlcv, parallel_mode=ParallelMode.SEQUENTIAL)
    candle_trades = len(candle_registry.get_closed_trades())
    candle_pnl = candle_registry.get_performance_metrics(100000.0)["total_pnl"]

    tick_strategy = build_tick_strategy(
        "TickMaBreakout",
        {
            "short_period": 5,
            "long_period": 10,
            "threshold": 0.0,
            "sl_points": 0.0,
            "tp_points": 0.0,
        },
        "TEST",
    )
    ticks = _ticks_from_prices(close, spread=0.0, msc_per_tick=3600000)
    tick_engine = TickBacktestEngine(
        strategy=tick_strategy,
        sizing_config=FixedQuantityPositionSizing(quantity=1.0),
        initial_capital=100000.0,
        point_value=1.0,
        symbol="TEST",
    )
    tick_registry = tick_engine.run(ticks, parallel_mode=ParallelMode.SEQUENTIAL)
    tick_trades = len(tick_registry.get_closed_trades())
    tick_pnl = tick_registry.get_performance_metrics(100000.0)["total_pnl"]

    assert abs(tick_trades - candle_trades) <= 3
    assert abs(tick_pnl - candle_pnl) <= max(5.0, abs(candle_pnl) * 0.25)
