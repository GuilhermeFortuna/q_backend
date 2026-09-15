"""Columnar strategy signal contract and shared queued-signal consumer (Q-024)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np
import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction, Trade

if TYPE_CHECKING:
    from q_backend.backtesting.strategy import TradingStrategy

SIGNAL_ENTRY: Final = "q_signal_entry"
SIGNAL_EXIT_LONG: Final = "q_signal_exit_long"
SIGNAL_EXIT_SHORT: Final = "q_signal_exit_short"
SIGNAL_STRENGTH: Final = "q_signal_strength"
SIGNAL_COLUMNS: Final = (SIGNAL_ENTRY, SIGNAL_EXIT_LONG, SIGNAL_EXIT_SHORT, SIGNAL_STRENGTH)
BAR_INDEX: Final = "bar_index"


class SignalContractError(ValueError):
    """Raised when signal columns are missing, mistyped, or out of range."""


def _require_bool_series(series: pd.Series, *, column: str, strategy_name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise SignalContractError(f"{strategy_name}: {column} must be a bool Series, got {type(series).__name__}")
    if series.dtype != bool and series.dtype != np.bool_:
        raise SignalContractError(f"{strategy_name}: {column} must be bool dtype, got {series.dtype}")
    if bool(series.isna().any()):
        raise SignalContractError(f"{strategy_name}: {column} contains NaN")
    return series


def _as_bool_series(
    value: pd.Series | bool,
    index: pd.Index,
    *,
    column: str,
    strategy_name: str,
) -> pd.Series:
    if isinstance(value, bool):
        return pd.Series(value, index=index, dtype=bool)
    return _require_bool_series(value, column=column, strategy_name=strategy_name)


def write_signal_columns(
    df: pd.DataFrame,
    *,
    entry_long: pd.Series,
    entry_short: pd.Series,
    exit_long: pd.Series | bool,
    exit_short: pd.Series | bool,
    strength: pd.Series | float = 1.0,
    strategy_name: str = "strategy",
) -> pd.DataFrame:
    """Write the four ``q_signal_*`` decision columns onto ``df`` (mutates and returns it)."""
    entry_long = _require_bool_series(entry_long, column="entry_long", strategy_name=strategy_name)
    entry_short = _require_bool_series(entry_short, column="entry_short", strategy_name=strategy_name)
    exit_long_s = _as_bool_series(exit_long, df.index, column=SIGNAL_EXIT_LONG, strategy_name=strategy_name)
    exit_short_s = _as_bool_series(exit_short, df.index, column=SIGNAL_EXIT_SHORT, strategy_name=strategy_name)

    # Long wins where both triggers are True (RSI double-trigger quirk).
    entry = np.zeros(len(df), dtype=np.int8)
    entry[entry_short.to_numpy(dtype=bool, copy=False)] = -1
    entry[entry_long.to_numpy(dtype=bool, copy=False)] = 1

    if isinstance(strength, (int, float)):
        strength_arr = np.full(len(df), float(strength), dtype=np.float64)
    else:
        if not isinstance(strength, pd.Series):
            raise SignalContractError(
                f"{strategy_name}: strength must be float or Series, got {type(strength).__name__}"
            )
        strength_arr = strength.to_numpy(dtype=np.float64, copy=True)

    out_strength = np.zeros(len(df), dtype=np.float64)
    has_entry = entry != 0
    out_strength[has_entry] = strength_arr[has_entry]

    df[SIGNAL_ENTRY] = entry
    df[SIGNAL_EXIT_LONG] = exit_long_s.to_numpy(dtype=bool, copy=False)
    df[SIGNAL_EXIT_SHORT] = exit_short_s.to_numpy(dtype=bool, copy=False)
    df[SIGNAL_STRENGTH] = out_strength
    return df


def validate_signal_columns(
    frame: pd.DataFrame,
    *,
    strategy_name: str,
    holding_period_bars: int | None,
) -> None:
    """Fail closed if signal columns are missing, mistyped, or out of contract range."""
    for column in SIGNAL_COLUMNS:
        if column not in frame.columns:
            raise SignalContractError(f"{strategy_name}: missing required signal column {column}")

    entry = frame[SIGNAL_ENTRY]
    if entry.dtype != np.int8:
        raise SignalContractError(f"{strategy_name}: {SIGNAL_ENTRY} must be int8, got {entry.dtype}")

    for column in (SIGNAL_EXIT_LONG, SIGNAL_EXIT_SHORT):
        series = frame[column]
        if series.dtype != bool and series.dtype != np.bool_:
            raise SignalContractError(f"{strategy_name}: {column} must be bool dtype, got {series.dtype}")

    strength = frame[SIGNAL_STRENGTH]
    if strength.dtype != np.float64:
        raise SignalContractError(f"{strategy_name}: {SIGNAL_STRENGTH} must be float64, got {strength.dtype}")

    entry_vals = entry.to_numpy()
    strength_vals = strength.to_numpy(dtype=np.float64, copy=False)
    has_entry = entry_vals != 0
    if has_entry.any():
        entry_strength = strength_vals[has_entry]
        if np.isnan(entry_strength).any():
            raise SignalContractError(f"{strategy_name}: {SIGNAL_STRENGTH} contains NaN on an entry bar")
        if (entry_strength < 0.0).any() or (entry_strength > 1.0).any():
            raise SignalContractError(f"{strategy_name}: {SIGNAL_STRENGTH} out of [0, 1] on an entry bar")
    if np.isnan(strength_vals).any():
        raise SignalContractError(f"{strategy_name}: {SIGNAL_STRENGTH} contains NaN")

    if holding_period_bars is not None:
        if BAR_INDEX not in frame.columns:
            raise SignalContractError(f"{strategy_name}: missing required column {BAR_INDEX} for holding period")
        if bool(frame[SIGNAL_EXIT_LONG].to_numpy(dtype=bool, copy=False).any()) or bool(
            frame[SIGNAL_EXIT_SHORT].to_numpy(dtype=bool, copy=False).any()
        ):
            raise SignalContractError(
                f"{strategy_name}: exit columns must be all-False when " f"holding_period_bars={holding_period_bars}"
            )


@dataclass(frozen=True)
class SignalArrays:
    index: pd.DatetimeIndex
    entry: np.ndarray
    exit_long: np.ndarray
    exit_short: np.ndarray
    strength: np.ndarray
    bar_index: np.ndarray | None
    holding_period_bars: int | None


def signal_arrays(strategy: TradingStrategy, frame: pd.DataFrame) -> SignalArrays:
    """Validate once per frame and extract columnar decision arrays."""
    holding = getattr(strategy, "holding_period_bars", None)
    validate_signal_columns(
        frame,
        strategy_name=type(strategy).__name__,
        holding_period_bars=holding,
    )
    bar_index = None
    if holding is not None:
        bar_index = frame[BAR_INDEX].to_numpy(dtype=np.int64, copy=False)
    return SignalArrays(
        index=pd.DatetimeIndex(frame.index),
        entry=frame[SIGNAL_ENTRY].to_numpy(dtype=np.int8, copy=False),
        exit_long=frame[SIGNAL_EXIT_LONG].to_numpy(dtype=bool, copy=False),
        exit_short=frame[SIGNAL_EXIT_SHORT].to_numpy(dtype=bool, copy=False),
        strength=frame[SIGNAL_STRENGTH].to_numpy(dtype=np.float64, copy=False),
        bar_index=bar_index,
        holding_period_bars=holding,
    )


def evaluate_queued_signals(
    strategy: TradingStrategy,
    signals: SignalArrays,
    position: int,
    current_data: pd.Series,
    open_trades: list[Trade],
) -> tuple[list[Signal], list[Signal]]:
    """Mirror engine section D: exit rules, then strategy exits, then at most one entry."""
    if hasattr(strategy, "exit_strategy"):
        pending_exits = list(strategy.exit_strategy.check_exits(open_trades, current_data))
    else:
        pending_exits = []

    closed_symbols = {sig.symbol for sig in pending_exits}
    symbol = strategy.symbol
    holding = signals.holding_period_bars

    if holding is not None and signals.bar_index is not None:
        current_bar = int(signals.bar_index[position])
        entry_positions = signals.index.get_indexer([trade.entry_time for trade in open_trades])
        for trade, entry_pos in zip(open_trades, entry_positions, strict=True):
            if trade.symbol in closed_symbols or trade.symbol != symbol:
                continue
            if entry_pos < 0:
                continue
            entry_bar = int(signals.bar_index[entry_pos])
            if current_bar - entry_bar >= holding:
                pending_exits.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
                closed_symbols.add(symbol)
    else:
        exit_long = bool(signals.exit_long[position])
        exit_short = bool(signals.exit_short[position])
        for trade in open_trades:
            if trade.symbol in closed_symbols or trade.symbol != symbol:
                continue
            if trade.action == SignalAction.BUY and exit_long:
                pending_exits.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
                closed_symbols.add(symbol)
            elif trade.action == SignalAction.SELL and exit_short:
                pending_exits.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
                closed_symbols.add(symbol)

    pending_entries: list[Signal] = []
    entry = int(signals.entry[position])
    if entry == 1:
        pending_entries.append(
            Signal(
                symbol=symbol,
                action=SignalAction.BUY,
                strength=float(signals.strength[position]),
            )
        )
    elif entry == -1:
        pending_entries.append(
            Signal(
                symbol=symbol,
                action=SignalAction.SELL,
                strength=float(signals.strength[position]),
            )
        )

    return pending_exits, pending_entries
