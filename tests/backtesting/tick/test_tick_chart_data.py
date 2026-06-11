import numpy as np

from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays


def _ticks_from_prices(
    prices: np.ndarray,
    start_msc: int = 1_700_000_000_000,
    step_ms: int = 30_000,
) -> TickArrays:
    n = len(prices)
    time_msc = start_msc + np.arange(n, dtype=np.int64) * step_ms
    return TickArrays(
        time_msc=time_msc,
        bid=prices - 0.01,
        ask=prices + 0.01,
        last=prices.astype(np.float64),
        volume=np.ones(n, dtype=np.float64),
    )


def test_resample_ticks_to_m1_bars():
    prices = np.array([10.0, 11.0, 12.0, 13.0])
    ticks = _ticks_from_prices(prices, step_ms=60_000)
    strategy = build_tick_strategy(
        "TickMaBreakout",
        {"short_period": 2, "long_period": 3, "threshold": 0.0},
        "TEST",
    )

    chart = serialize_tick_chart_data(ticks, strategy, display_timeframe="M1")
    bars = chart["bars"]

    assert len(bars) == 4
    assert bars[0]["open"] == 10.0
    assert bars[0]["close"] == 10.0
    assert bars[1]["open"] == 11.0
    assert bars[3]["close"] == 13.0
    assert all(bar["timestamp"].endswith("Z") for bar in bars)


def test_indicator_values_align_with_bars():
    prices = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    ticks = _ticks_from_prices(prices, step_ms=60_000)
    strategy = build_tick_strategy(
        "TickMaBreakout",
        {"short_period": 2, "long_period": 3, "threshold": 0.0},
        "TEST",
    )

    chart = serialize_tick_chart_data(ticks, strategy, display_timeframe="M1")
    indicators = chart["indicators"]
    bars = chart["bars"]

    ma_short = next(ind for ind in indicators if ind["key"] == "ma_short")
    assert len(ma_short["values"]) == len(bars)
    assert ma_short["values"][0] is None
    assert ma_short["values"][1] is not None
