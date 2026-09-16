"""Unit tests for columnar signal writer, validator, and array consumer (Q-024)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.candle_kernel import evaluate_bar
from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.signal_columns import (
    BAR_INDEX,
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
    SIGNAL_STRENGTH,
    SignalArrays,
    SignalContractError,
    validate_signal_columns,
    write_signal_columns,
)


def _index(n: int = 10) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="h", tz=timezone.utc)


def _empty_frame(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame(index=_index(n))


def _bool_series(values: list[bool], index: pd.DatetimeIndex | None = None) -> pd.Series:
    idx = index if index is not None else _index(len(values))
    return pd.Series(values, index=idx, dtype=bool)


def _valid_frame(*, n: int = 10, entry: list[int] | None = None) -> pd.DataFrame:
    entry_vals = entry if entry is not None else [0] * n
    n = len(entry_vals)
    idx = _index(n)
    df = pd.DataFrame(index=idx)
    df[SIGNAL_ENTRY] = np.asarray(entry_vals, dtype=np.int8)
    df[SIGNAL_EXIT_LONG] = np.zeros(n, dtype=bool)
    df[SIGNAL_EXIT_SHORT] = np.zeros(n, dtype=bool)
    strength = np.zeros(n, dtype=np.float64)
    for i, e in enumerate(entry_vals):
        if e != 0:
            strength[i] = 1.0
    df[SIGNAL_STRENGTH] = strength
    return df


def _trade(
    *,
    symbol: str = "TEST",
    action: SignalAction = SignalAction.BUY,
    entry_time: datetime | None = None,
) -> Trade:
    return Trade(
        id="t1",
        order_id="o1",
        symbol=symbol,
        action=action,
        quantity=1.0,
        entry_time=entry_time or datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_price=100.0,
    )


def _ohlcv_frame(signals: SignalArrays) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": np.full(len(signals.index), 100.0),
            "high": np.full(len(signals.index), 101.0),
            "low": np.full(len(signals.index), 99.0),
            "close": np.full(len(signals.index), 100.0),
        },
        index=signals.index,
    )


def _strategy(symbol: str = "TEST", holding_period_bars: int | None = None) -> MagicMock:
    strategy = MagicMock()
    strategy.symbol = symbol
    strategy.holding_period_bars = holding_period_bars
    strategy.exit_strategy = None
    return strategy


def _arrays(
    *,
    n: int = 10,
    exit_long: list[bool] | None = None,
    exit_short: list[bool] | None = None,
    entry: list[int] | None = None,
    strength: list[float] | None = None,
    holding_period_bars: int | None = None,
    with_bar_index: bool = False,
) -> SignalArrays:
    idx = _index(n)
    entry_arr = np.asarray(entry if entry is not None else [0] * n, dtype=np.int8)
    strength_arr = np.asarray(
        strength if strength is not None else [0.0] * n,
        dtype=np.float64,
    )
    bar_index = np.arange(n, dtype=np.int64) if with_bar_index or holding_period_bars is not None else None
    return SignalArrays(
        index=idx,
        entry=entry_arr,
        exit_long=np.asarray(exit_long if exit_long is not None else [False] * n, dtype=bool),
        exit_short=np.asarray(exit_short if exit_short is not None else [False] * n, dtype=bool),
        strength=strength_arr,
        bar_index=bar_index,
        holding_period_bars=holding_period_bars,
    )


# --- write_signal_columns -----------------------------------------------------


def test_write_signal_columns_long_wins_when_both_triggers_true():
    df = _empty_frame(3)
    both = _bool_series([False, True, False])
    out = write_signal_columns(
        df,
        entry_long=both,
        entry_short=both,
        exit_long=False,
        exit_short=False,
    )
    assert out[SIGNAL_ENTRY].tolist() == [0, 1, 0]
    assert out[SIGNAL_ENTRY].dtype == np.int8


def test_write_signal_columns_strength_zero_where_no_entry():
    df = _empty_frame(3)
    entry_long = _bool_series([False, True, False])
    entry_short = _bool_series([False, False, False])
    strength = pd.Series([0.4, 0.4, 0.4], index=df.index, dtype=np.float64)
    out = write_signal_columns(
        df,
        entry_long=entry_long,
        entry_short=entry_short,
        exit_long=False,
        exit_short=False,
        strength=strength,
    )
    assert out[SIGNAL_STRENGTH].tolist() == [0.0, 0.4, 0.0]
    assert float(out[SIGNAL_STRENGTH].iloc[0]) == 0.0
    assert float(out[SIGNAL_STRENGTH].iloc[2]) == 0.0


def test_write_signal_columns_rejects_non_bool_trigger():
    df = _empty_frame(2)
    float_trigger = pd.Series([0.0, 1.0], index=df.index, dtype=np.float64)
    with pytest.raises(SignalContractError):
        write_signal_columns(
            df,
            entry_long=float_trigger,
            entry_short=_bool_series([False, False]),
            exit_long=False,
            exit_short=False,
        )


# --- validate_signal_columns --------------------------------------------------


def test_validate_raises_naming_strategy_and_missing_exit_short():
    df = _valid_frame()
    del df[SIGNAL_EXIT_SHORT]
    with pytest.raises(SignalContractError, match=r"(?=.*FakeStrat)(?=.*q_signal_exit_short)"):
        validate_signal_columns(df, strategy_name="FakeStrat", holding_period_bars=None)


def test_validate_raises_for_float32_strength():
    df = _valid_frame(entry=[0, 1, 0])
    df[SIGNAL_STRENGTH] = df[SIGNAL_STRENGTH].astype(np.float32)
    with pytest.raises(SignalContractError, match=SIGNAL_STRENGTH):
        validate_signal_columns(df, strategy_name="S", holding_period_bars=None)


def test_validate_raises_for_strength_out_of_range_on_entry():
    df = _valid_frame(entry=[0, 1, 0])
    df.loc[df.index[1], SIGNAL_STRENGTH] = 1.5
    with pytest.raises(SignalContractError, match=SIGNAL_STRENGTH):
        validate_signal_columns(df, strategy_name="S", holding_period_bars=None)


def test_validate_raises_for_nan_strength_on_entry():
    df = _valid_frame(entry=[0, 1, 0])
    df.loc[df.index[1], SIGNAL_STRENGTH] = np.nan
    with pytest.raises(SignalContractError, match=SIGNAL_STRENGTH):
        validate_signal_columns(df, strategy_name="S", holding_period_bars=None)


def test_validate_raises_for_true_exit_when_holding_period_declared():
    df = _valid_frame()
    df[BAR_INDEX] = np.arange(len(df), dtype=np.int64)
    df.loc[df.index[2], SIGNAL_EXIT_LONG] = True
    with pytest.raises(SignalContractError):
        validate_signal_columns(df, strategy_name="S", holding_period_bars=10)


def test_validate_raises_for_missing_bar_index_when_holding_period_declared():
    df = _valid_frame()
    with pytest.raises(SignalContractError, match=BAR_INDEX):
        validate_signal_columns(df, strategy_name="S", holding_period_bars=10)


# --- evaluate_bar --------------------------------------------------------------


def test_evaluate_exit_long_closes_open_buy_on_strategy_symbol():
    signals = _arrays(exit_long=[False] * 5 + [True] + [False] * 4)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST")
    trade = _trade(symbol="TEST", action=SignalAction.BUY, entry_time=signals.index[0])
    exits, entries = evaluate_bar(strategy, frame, signals, 5, [trade])
    assert entries == []
    assert len(exits) == 1
    assert exits[0].action == SignalAction.CLOSE
    assert exits[0].symbol == "TEST"
    assert exits[0].exit_reason is None


def test_evaluate_exit_long_ignores_open_sell():
    signals = _arrays(exit_long=[False] * 5 + [True] + [False] * 4)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST")
    trade = _trade(symbol="TEST", action=SignalAction.SELL, entry_time=signals.index[0])
    exits, entries = evaluate_bar(strategy, frame, signals, 5, [trade])
    assert exits == []
    assert entries == []


def test_evaluate_exit_long_ignores_buy_on_other_symbol():
    signals = _arrays(exit_long=[False] * 5 + [True] + [False] * 4)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST")
    trade = _trade(symbol="OTHER", action=SignalAction.BUY, entry_time=signals.index[0])
    exits, entries = evaluate_bar(strategy, frame, signals, 5, [trade])
    assert len(exits) == 1
    assert exits[0].action == SignalAction.CLOSE
    assert entries == []


def test_evaluate_exit_rule_precedes_strategy_exit_no_duplicate():
    signals = _arrays(exit_long=[False] * 5 + [True] + [False] * 4)
    frame = _ohlcv_frame(signals)
    frame.loc[frame.index[5], "low"] = 80.0
    strategy = _strategy("TEST")
    strategy.exit_strategy = ExitStrategy(stop_loss_pct=0.02)
    trade = _trade(symbol="TEST", action=SignalAction.BUY, entry_time=signals.index[0])
    exits, entries = evaluate_bar(strategy, frame, signals, 5, [trade])
    assert entries == []
    assert len(exits) == 1
    assert exits[0].exit_reason == "fixed_sl"


def test_evaluate_holding_period_closes_at_period_not_before():
    signals = _arrays(n=8, holding_period_bars=2, with_bar_index=True)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST", holding_period_bars=2)
    entry_time = signals.index[3]
    trade = _trade(symbol="TEST", action=SignalAction.BUY, entry_time=entry_time)

    exits_at_4, _ = evaluate_bar(strategy, frame, signals, 4, [trade])
    assert exits_at_4 == []

    exits_at_5, _ = evaluate_bar(strategy, frame, signals, 5, [trade])
    assert len(exits_at_5) == 1
    assert exits_at_5[0].action == SignalAction.CLOSE
    assert exits_at_5[0].exit_reason is None


def test_evaluate_holding_period_off_grid_entry_never_closes():
    signals = _arrays(n=8, holding_period_bars=2, with_bar_index=True)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST", holding_period_bars=2)
    off_grid = signals.index[3] + timedelta(minutes=30)
    trade = _trade(symbol="TEST", action=SignalAction.BUY, entry_time=off_grid)
    for position in range(len(signals.index)):
        exits, _ = evaluate_bar(strategy, frame, signals, position, [trade])
        assert exits == [], f"unexpected close at position {position}"


def test_evaluate_entry_short_with_strength():
    entry = [0] * 10
    entry[7] = -1
    strength = [0.0] * 10
    strength[7] = 0.25
    signals = _arrays(entry=entry, strength=strength)
    frame = _ohlcv_frame(signals)
    strategy = _strategy("TEST")
    exits, entries = evaluate_bar(strategy, frame, signals, 7, [])
    assert exits == []
    assert len(entries) == 1
    assert entries[0].action == SignalAction.SELL
    assert entries[0].strength == 0.25
    assert entries[0].symbol == "TEST"
