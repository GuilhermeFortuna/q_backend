"""Per-strategy columnar signal contract mapping (Q-024 step 6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401 — register built-ins
from q_backend.backtesting.composite_entry import CompositeEntryStrategy
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.signal_columns import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
    SIGNAL_STRENGTH,
)
from q_backend.backtesting.strategies.bollinger_reversion import BollingerReversionStrategy
from q_backend.backtesting.strategies.donchian_breakout import DonchianBreakoutStrategy
from q_backend.backtesting.strategies.fma import FMAStrategy
from q_backend.backtesting.strategies.gatev_pairs import GatevPairsStrategy
from q_backend.backtesting.strategies.hurst_trend_blend import HurstTrendBlendStrategy
from q_backend.backtesting.strategies.macd import MACDStrategy
from q_backend.backtesting.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
from q_backend.backtesting.strategies.trb import TRBStrategy
from q_backend.backtesting.strategies.tsmom import TSMOMStrategy
from q_backend.backtesting.strategies.vma import VMAStrategy
from q_backend.backtesting.strategy import MACrossoverStrategy, TradingStrategy
from q_backend.backtesting.strategy_registry import default_params_for

from backtesting.test_goldens import SYMBOL, synthetic_ohlcv
from backtesting.test_signal_baseline import (
    BASELINE_CASE_NAMES,
    BASELINE_CASES,
    HOLDING_PERIOD_CASES,
)


def _bool_col(frame: pd.DataFrame, name: str) -> pd.Series:
    return frame[name].astype(bool)


def _expected_entry(entry_long: pd.Series, entry_short: pd.Series) -> np.ndarray:
    entry = np.zeros(len(entry_long), dtype=np.int8)
    entry[entry_short.to_numpy(dtype=bool, copy=False)] = -1
    entry[entry_long.to_numpy(dtype=bool, copy=False)] = 1
    return entry


def _expected_strength(entry: np.ndarray, strength: pd.Series | float) -> np.ndarray:
    if isinstance(strength, (int, float)):
        strength_arr = np.full(len(entry), float(strength), dtype=np.float64)
    else:
        strength_arr = strength.to_numpy(dtype=np.float64, copy=True)
    out = np.zeros(len(entry), dtype=np.float64)
    has_entry = entry != 0
    out[has_entry] = strength_arr[has_entry]
    return out


def expected_signal_mapping(strategy: TradingStrategy, frame: pd.DataFrame) -> dict[str, Any]:
    """Reproduce the plan mapping table over legacy trigger columns."""
    if isinstance(strategy, BollingerReversionStrategy):
        entry_long = _bool_col(frame, "buy_signal")
        entry_short = _bool_col(frame, "sell_signal")
        exit_long = _bool_col(frame, "exit_long_signal")
        exit_short = _bool_col(frame, "exit_short_signal")
        strength: pd.Series | float = 1.0
    elif isinstance(strategy, (FMAStrategy, TRBStrategy)):
        entry_long = _bool_col(frame, "buy_signal")
        entry_short = _bool_col(frame, "sell_signal")
        exit_long = pd.Series(False, index=frame.index, dtype=bool)
        exit_short = pd.Series(False, index=frame.index, dtype=bool)
        strength = 1.0
    elif isinstance(strategy, (TSMOMStrategy, HurstTrendBlendStrategy)):
        rebalance = _bool_col(frame, "rebalance")
        mom = frame["momentum"]
        strength = frame["signal_strength"]
        if strategy.rebalance_on_every_bar:
            entry_long = (rebalance & (mom > 0)).astype(bool)
            entry_short = (rebalance & (mom < 0)).astype(bool)
            exit_long = (_bool_col(frame, "sell_signal") | rebalance).astype(bool)
            exit_short = (_bool_col(frame, "buy_signal") | rebalance).astype(bool)
        else:
            entry_long = _bool_col(frame, "buy_signal")
            entry_short = _bool_col(frame, "sell_signal")
            exit_long = _bool_col(frame, "sell_signal")
            exit_short = _bool_col(frame, "buy_signal")
    elif isinstance(strategy, GatevPairsStrategy):
        entry_long = _bool_col(frame, "buy_signal")
        entry_short = _bool_col(frame, "sell_signal")
        exit_long = _bool_col(frame, "exit_signal")
        exit_short = _bool_col(frame, "exit_signal")
        strength = 1.0
    elif isinstance(strategy, CompositeEntryStrategy):
        entry_long = _bool_col(frame, "net_long_signal")
        entry_short = _bool_col(frame, "net_short_signal")
        exit_long = (frame["net_stance"] == -1).astype(bool)
        exit_short = (frame["net_stance"] == 1).astype(bool)
        strength = 1.0
    elif isinstance(strategy, CompositeStrategy):
        entry_long = _bool_col(frame, "entry_long_signal")
        entry_short = _bool_col(frame, "entry_short_signal")
        if strategy.plan.fixed_holding_period is not None:
            exit_long = pd.Series(False, index=frame.index, dtype=bool)
            exit_short = pd.Series(False, index=frame.index, dtype=bool)
        else:
            if "exit_long_signal" in frame.columns:
                exit_long = _bool_col(frame, "exit_long_signal")
            else:
                exit_long = pd.Series(False, index=frame.index, dtype=bool)
            if "exit_short_signal" in frame.columns:
                exit_short = _bool_col(frame, "exit_short_signal")
            else:
                exit_short = pd.Series(False, index=frame.index, dtype=bool)
        strength = 1.0
    elif isinstance(
        strategy,
        (
            MACrossoverStrategy,
            MACDStrategy,
            RSIMeanReversionStrategy,
            DonchianBreakoutStrategy,
            VMAStrategy,
        ),
    ):
        entry_long = _bool_col(frame, "buy_signal")
        entry_short = _bool_col(frame, "sell_signal")
        exit_long = _bool_col(frame, "sell_signal")
        exit_short = _bool_col(frame, "buy_signal")
        strength = 1.0
    else:
        raise TypeError(f"no mapping for {type(strategy).__name__}")

    entry = _expected_entry(entry_long, entry_short)
    return {
        SIGNAL_ENTRY: entry,
        SIGNAL_EXIT_LONG: exit_long.to_numpy(dtype=bool, copy=False),
        SIGNAL_EXIT_SHORT: exit_short.to_numpy(dtype=bool, copy=False),
        SIGNAL_STRENGTH: _expected_strength(entry, strength),
    }


@pytest.mark.parametrize("name", BASELINE_CASE_NAMES)
def test_signal_contract_columns_match_mapping(name: str) -> None:
    case = BASELINE_CASES[name]
    strategy = case.make_strategy()
    frame = augment_indicator_frame(strategy, case.data())

    for column, dtype in (
        (SIGNAL_ENTRY, np.int8),
        (SIGNAL_EXIT_LONG, bool),
        (SIGNAL_EXIT_SHORT, bool),
        (SIGNAL_STRENGTH, np.float64),
    ):
        assert column in frame.columns, f"{name}: missing {column}"
        assert frame[column].dtype == dtype, f"{name}: {column} dtype {frame[column].dtype!r} != {dtype!r}"

    expected = expected_signal_mapping(strategy, frame)
    np.testing.assert_array_equal(frame[SIGNAL_ENTRY].to_numpy(), expected[SIGNAL_ENTRY], err_msg=name)
    np.testing.assert_array_equal(frame[SIGNAL_EXIT_LONG].to_numpy(), expected[SIGNAL_EXIT_LONG], err_msg=name)
    np.testing.assert_array_equal(frame[SIGNAL_EXIT_SHORT].to_numpy(), expected[SIGNAL_EXIT_SHORT], err_msg=name)
    np.testing.assert_allclose(frame[SIGNAL_STRENGTH].to_numpy(), expected[SIGNAL_STRENGTH], err_msg=name)


@pytest.mark.parametrize("name", sorted(HOLDING_PERIOD_CASES))
def test_holding_period_bars_and_false_exits(name: str) -> None:
    case = BASELINE_CASES[name]
    strategy = case.make_strategy()
    assert strategy.holding_period_bars == 10
    frame = augment_indicator_frame(strategy, case.data())
    assert not frame[SIGNAL_EXIT_LONG].any()
    assert not frame[SIGNAL_EXIT_SHORT].any()


def test_rsi_double_trigger_prefers_long() -> None:
    """RSI 25→75 fires both triggers; long wins so q_signal_entry == 1."""
    n = 40
    data = synthetic_ohlcv(n)
    rsi_vals = np.full(n, 50.0)
    bar = 25
    rsi_vals[bar - 1] = 25.0
    rsi_vals[bar] = 75.0

    def _fake_rsi(close: pd.Series, period: int) -> pd.Series:
        return pd.Series(rsi_vals, index=close.index, dtype=float)

    strategy = build_strategy("RSIMeanReversion", default_params_for("RSIMeanReversion"), SYMBOL)
    with patch("q_backend.backtesting.strategies.rsi_mean_reversion.compute_rsi", _fake_rsi):
        frame = strategy.compute_indicators(data)

    assert bool(frame["buy_signal"].iloc[bar])
    assert bool(frame["sell_signal"].iloc[bar])
    assert int(frame[SIGNAL_ENTRY].iloc[bar]) == 1


def test_composite_entry_signal_columns_are_prefix_causal() -> None:
    case = BASELINE_CASES["composite_or"]
    strategy = case.make_strategy()
    data = case.data()
    full = strategy.compute_indicators(data)
    signal_cols = [SIGNAL_ENTRY, SIGNAL_EXIT_LONG, SIGNAL_EXIT_SHORT, SIGNAL_STRENGTH]
    for k in (205, 230, 399):
        prefix = strategy.compute_indicators(data.iloc[:k].copy())
        for col in signal_cols:
            expected = full[col].iloc[:k]
            actual = prefix[col]
            if col == SIGNAL_STRENGTH:
                pd.testing.assert_series_equal(expected, actual, check_names=False, rtol=1e-9, atol=1e-9)
            else:
                np.testing.assert_array_equal(
                    expected.to_numpy(),
                    actual.to_numpy(),
                    err_msg=f"composite_or {col} changes at prefix {k}",
                )
