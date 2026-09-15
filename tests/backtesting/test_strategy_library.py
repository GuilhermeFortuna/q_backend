import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.signal_columns import SIGNAL_ENTRY, SIGNAL_EXIT_LONG


def _assert_no_lookahead(strategy, df: pd.DataFrame, signal_col: str) -> None:
    full = strategy.compute_indicators(df.copy())
    cutoff = len(df) - 5
    if cutoff < 2:
        pytest.skip("Series too short for lookahead check")
    partial = strategy.compute_indicators(df.iloc[:cutoff].copy())
    pd.testing.assert_series_equal(
        full[signal_col].iloc[:cutoff],
        partial[signal_col],
        check_names=False,
    )


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    return pd.DataFrame({"close": closes, "high": highs, "low": lows})


def test_rsi_mean_reversion_indicators_and_signals():
    strategy = build_strategy(
        "RSIMeanReversion",
        {"period": 3, "oversold": 30.0, "overbought": 70.0},
        "TEST",
    )
    closes = [100.0] * 8 + [90.0, 85.0, 80.0, 95.0, 110.0, 120.0, 130.0]
    df = _make_ohlcv(closes)
    result = strategy.compute_indicators(df)

    assert "rsi" in result.columns
    assert result["buy_signal"].dtype == bool
    assert result["buy_signal"].any() or result["sell_signal"].any()

    buy_rows = result.index[result["buy_signal"]]
    if len(buy_rows) > 0:
        assert result.loc[buy_rows[0], SIGNAL_ENTRY] == 1

    _assert_no_lookahead(strategy, df, "buy_signal")


def test_bollinger_reversion_indicators_and_signals():
    strategy = build_strategy("BollingerReversion", {"period": 5, "num_std": 2.0}, "TEST")
    closes = [100.0] * 10 + [80.0, 75.0, 70.0, 85.0, 100.0, 110.0]
    df = _make_ohlcv(closes)
    result = strategy.compute_indicators(df)

    for col in ("bb_upper", "bb_middle", "bb_lower", "buy_signal", "sell_signal"):
        assert col in result.columns

    specs = strategy.get_chart_indicators()
    assert {spec.key for spec in specs} == {"bb_upper", "bb_middle", "bb_lower"}

    _assert_no_lookahead(strategy, df, "buy_signal")


def test_macd_indicators_and_signals():
    strategy = build_strategy(
        "MACD",
        {"fast_period": 3, "slow_period": 6, "signal_period": 3},
        "TEST",
    )
    closes = list(np.linspace(100, 120, 20)) + list(np.linspace(120, 90, 20))
    df = _make_ohlcv(closes)
    result = strategy.compute_indicators(df)

    for col in ("macd", "macd_signal", "macd_histogram", "buy_signal", "sell_signal"):
        assert col in result.columns

    if result["buy_signal"].any():
        row = result.loc[result.index[result["buy_signal"]][0]]
        assert row[SIGNAL_ENTRY] == 1

    _assert_no_lookahead(strategy, df, "buy_signal")


def test_donchian_breakout_indicators_and_signals():
    strategy = build_strategy("DonchianBreakout", {"period": 5}, "TEST")
    closes = [100.0] * 10 + [105.0, 110.0, 115.0, 120.0, 125.0, 130.0]
    df = _make_ohlcv(closes)
    result = strategy.compute_indicators(df)

    for col in ("donchian_upper", "donchian_lower", "buy_signal", "sell_signal"):
        assert col in result.columns

    if result["buy_signal"].any():
        row = result.loc[result.index[result["buy_signal"]][0]]
        assert row[SIGNAL_ENTRY] == 1

    _assert_no_lookahead(strategy, df, "buy_signal")


def test_macd_exit_closes_open_trade():
    strategy = build_strategy(
        "MACD",
        {"fast_period": 3, "slow_period": 6, "signal_period": 3},
        "TEST",
    )
    closes = list(np.linspace(100, 120, 20)) + list(np.linspace(120, 90, 20))
    df = _make_ohlcv(closes)
    result = strategy.compute_indicators(df)

    sell_rows = result.index[result["sell_signal"]]
    if len(sell_rows) == 0:
        pytest.skip("No sell signal in crafted series")

    row = result.loc[sell_rows[0]]
    assert row[SIGNAL_EXIT_LONG] is True or row[SIGNAL_EXIT_LONG] == True
    assert row[SIGNAL_ENTRY] == -1
