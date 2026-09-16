"""Boundary bridge from the candle engine to q_core's candle kernel."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
import q_core

from q_backend.backtesting.position_sizing import (
    FixedQuantitySizer,
    FixedSafetyMarginSizer,
    InverseVolatilitySizer,
    PositionSizer,
)
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.models import Order, OrderAction, OrderType, Signal, SignalAction, Trade
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.signal_columns import SignalArrays
from q_backend.backtesting.strategy import TradingStrategy

REQUIRED_ENGINE_FUNCTIONS: Final = ("run_candle", "DecisionStep", "size_entry", "required_columns")


def check_engine(module: Any) -> None:
    """Raise a startup error if the installed q_core lacks Q-027's candle API."""
    target = getattr(module, "engine", module)
    version_fn = getattr(module, "version", None)
    version = version_fn() if callable(version_fn) else getattr(module, "__version__", "unknown")
    missing = [name for name in REQUIRED_ENGINE_FUNCTIONS if not hasattr(target, name)]
    if missing:
        raise ImportError(f"q_core {version} is missing required candle kernel functions: {', '.join(missing)}")


check_engine(q_core)
engine: Any = q_core.engine


@dataclass(frozen=True)
class KernelSizing:
    mapping: dict[str, object]
    point_value: float
    needs_volatility: bool


def sizer_to_kernel(sizer: PositionSizer) -> KernelSizing:
    """Map only the three exact legacy sizer implementations to q_core config."""
    if type(sizer) is FixedQuantitySizer:
        return KernelSizing(
            {
                "type": "fixed_quantity",
                "quantity": sizer.quantity,
                "scale_by_signal_strength": sizer.scale_by_signal_strength,
            },
            1.0,
            False,
        )
    if type(sizer) is FixedSafetyMarginSizer:
        return KernelSizing(
            {
                "type": "fixed_safety_margin",
                "safety_margin_per_contract": sizer.safety_margin_per_contract,
                "max_contracts": sizer.max_contracts,
                "min_contracts": sizer.min_contracts,
                "scale_by_signal_strength": sizer.scale_by_signal_strength,
            },
            1.0,
            False,
        )
    if type(sizer) is InverseVolatilitySizer:
        return KernelSizing(
            {
                "type": "inverse_volatility",
                "target_volatility_pct": sizer.target_volatility_pct,
                "max_contracts": sizer.max_contracts,
                "min_contracts": sizer.min_contracts,
                "scale_by_signal_strength": sizer.scale_by_signal_strength,
            },
            sizer.point_value,
            True,
        )
    raise TypeError(f"q_core candle kernel does not support position sizer {type(sizer).__name__}")


def parse_day_trade_times(start: str, end: str, close: str) -> tuple[int, int, int]:
    """Parse the existing permissive HH:MM inputs, preserving its observable error."""
    try:

        def as_us(value: str) -> int:
            hours, minutes = value.split(":")
            return (datetime.time(int(hours), int(minutes)).hour * 3_600 + int(minutes) * 60) * 1_000_000

        return as_us(start), as_us(end), as_us(close)
    except Exception as exc:
        raise ValueError(
            f"Invalid day trade time config (start={start}, end={end}, close={close}). Must be HH:MM format."
        ) from exc


def wall_clock_us(index: pd.DatetimeIndex) -> np.ndarray:
    """Return the index's own wall-clock timestamps, floored to microseconds."""
    wall_clock = index.tz_localize(None) if index.tz is not None else index
    return np.asarray(wall_clock.as_unit("ns").asi8 // 1_000, dtype=np.int64)


@dataclass(frozen=True)
class ChunkRun:
    trades: dict[str, np.ndarray]
    exit_reason_text: list[str | None]


def _column(frame: pd.DataFrame, name: str) -> np.ndarray | None:
    if name not in frame:
        return None
    return np.ascontiguousarray(pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan))


def _exit_params(source: dict[str, object]) -> dict[str, object]:
    from q_backend.backtesting.exit_rules.registry import all_param_specs

    return {spec.name: source[spec.name] for spec in all_param_specs() if spec.name in source}


def _exit_params_from_strategy(strategy: TradingStrategy) -> dict[str, object]:
    exit_strategy = getattr(strategy, "exit_strategy", None)
    source = exit_strategy.params if exit_strategy is not None else strategy.parameters
    return _exit_params(source)


def _columns_for_frame(frame: pd.DataFrame, exit_params: dict[str, object]) -> dict[str, np.ndarray]:
    return {
        name: values for name in engine.required_columns(exit_params) if (values := _column(frame, name)) is not None
    }


def _trade_tuples(
    open_trades: list[Trade],
    index: pd.DatetimeIndex,
) -> list[tuple[str, int, float, int | None]]:
    trades: list[tuple[str, int, float, int | None]] = []
    for trade in open_trades:
        entry_pos = int(index.get_indexer([trade.entry_time])[0])
        entry_bar = entry_pos if entry_pos >= 0 else None
        side = 1 if trade.action == OrderAction.BUY else -1
        trades.append((trade.id, side, trade.entry_price, entry_bar))
    return trades


def enabled_rule_ids(params: dict[str, object]) -> list[str]:
    return list(engine.enabled_rules(_exit_params(params)))


def required_exit_columns(params: dict[str, object]) -> list[str]:
    return list(engine.required_columns(_exit_params(params)))


def exit_step(exit_strategy: Any) -> Any:
    """Return the q_core DecisionStep owned by an ExitStrategy."""
    return exit_strategy._step


def evaluate_bar(
    strategy: TradingStrategy,
    frame: pd.DataFrame,
    signals: SignalArrays,
    position: int,
    open_trades: list[Trade],
) -> tuple[list[Signal], list[Signal]]:
    """Produce queued exits and entries for one bar through q_core section D."""
    exit_strategy = getattr(strategy, "exit_strategy", None)
    if exit_strategy is not None:
        exit_strategy._sync_open_trade_ids(open_trades)
        step = exit_strategy._step
        exit_params = _exit_params(exit_strategy.params)
    else:
        step = engine.DecisionStep(exit_params={})
        exit_params = {}

    columns = _columns_for_frame(frame, exit_params)
    trades = _trade_tuples(open_trades, signals.index)
    exits, entry = step.decide(
        bar=position,
        trades=trades,
        close=_column(frame, "close"),
        high=_column(frame, "high"),
        low=_column(frame, "low"),
        entry=np.ascontiguousarray(signals.entry),
        exit_long=np.ascontiguousarray(signals.exit_long),
        exit_short=np.ascontiguousarray(signals.exit_short),
        strength=np.ascontiguousarray(signals.strength),
        bar_index=None if signals.bar_index is None else np.ascontiguousarray(signals.bar_index),
        columns=columns,
        holding_period_bars=signals.holding_period_bars,
    )
    exit_signals = [
        Signal(symbol=strategy.symbol, action=SignalAction.CLOSE, exit_reason=reason) for _id, reason in exits
    ]
    entry_signals: list[Signal] = []
    if entry is not None:
        entry_signals.append(
            Signal(
                symbol=strategy.symbol,
                action=SignalAction.BUY if entry[0] == 1 else SignalAction.SELL,
                strength=entry[1],
            )
        )
    return exit_signals, entry_signals


def _read_volatility(current_data: pd.Series | None) -> float | None:
    if current_data is None:
        return None
    vol = current_data.get("volatility")
    if vol is None or pd.isna(vol):
        return None
    try:
        vol_f = float(vol)
    except (TypeError, ValueError):
        return None
    if vol_f <= 0 or np.isnan(vol_f):
        return None
    return vol_f


def size_order(
    sizing: KernelSizing,
    signal: Signal,
    price: float,
    capital: float,
    *,
    current_data: pd.Series | None = None,
) -> float | None:
    import math

    volatility = _read_volatility(current_data) if sizing.needs_volatility else None
    quantity = engine.size_entry(
        sizing.mapping,
        point_value=sizing.point_value,
        strength=float(getattr(signal, "strength", 1.0)),
        price=price,
        capital=capital,
        volatility=volatility,
    )
    if quantity is None:
        return None
    qty = float(math.floor(quantity))
    return qty if qty > 0.0 else None


def max_position(
    sizing: KernelSizing,
    price: float,
    capital: float,
) -> float | None:
    return engine.max_position(
        sizing.mapping,
        point_value=sizing.point_value,
        price=price,
        capital=capital,
    )


def run_chunk(
    strategy: TradingStrategy,
    chunk: pd.DataFrame,
    signals: SignalArrays,
    *,
    sizing: KernelSizing,
    initial_capital: float,
    point_value: float,
    costs: TransactionCostConfig | None,
    day_trade_us: tuple[int, int, int] | None,
    force_close_at_end: bool,
    trade_start: datetime.datetime | None,
) -> ChunkRun:
    """Run one already-augmented candle chunk through q_core without row objects."""
    from q_backend.backtesting.exit_rules.registry import all_param_specs

    source_params = getattr(getattr(strategy, "exit_strategy", None), "params", strategy.parameters)
    exit_params = {spec.name: source_params[spec.name] for spec in all_param_specs() if spec.name in source_params}
    columns = {
        name: values for name in engine.required_columns(exit_params) if (values := _column(chunk, name)) is not None
    }
    tradable = None if trade_start is None else np.ascontiguousarray(~(chunk.index < trade_start), dtype=bool)
    result = engine.run_candle(
        time_us=np.ascontiguousarray(wall_clock_us(signals.index)),
        open=_column(chunk, "open"),
        high=_column(chunk, "high"),
        low=_column(chunk, "low"),
        close=_column(chunk, "close"),
        entry=np.ascontiguousarray(signals.entry),
        exit_long=np.ascontiguousarray(signals.exit_long),
        exit_short=np.ascontiguousarray(signals.exit_short),
        strength=np.ascontiguousarray(signals.strength),
        bar_index=None if signals.bar_index is None else np.ascontiguousarray(signals.bar_index),
        volatility=_column(chunk, "volatility") if sizing.needs_volatility else None,
        tradable=tradable,
        columns=columns,
        initial_capital=initial_capital,
        point_value=point_value,
        costs=None if costs is None else (costs.cost_per_contract, costs.cost_bps),
        sizing=sizing.mapping,
        sizing_point_value=sizing.point_value,
        holding_period_bars=signals.holding_period_bars,
        exit_params=exit_params,
        day_trade_us=day_trade_us,
        force_close_at_end=force_close_at_end,
    )
    return ChunkRun(
        {
            name: np.asarray(result[name])
            for name in ("entry_bar", "exit_bar", "side", "quantity", "entry_price", "exit_price", "commission", "pnl")
        },
        list(result["exit_reason_text"]),
    )


def ledger_to_registry(run: ChunkRun, index: pd.DatetimeIndex, *, symbol: str, point_value: float) -> TradeRegistry:
    """Recreate the public registry objects, asserting q_core and registry PnL agree."""
    registry = TradeRegistry()
    for i, entry_bar in enumerate(run.trades["entry_bar"]):
        action = OrderAction.BUY if int(run.trades["side"][i]) == 1 else OrderAction.SELL
        quantity = float(run.trades["quantity"][i])
        order = Order(
            id=str(uuid.uuid4()), symbol=symbol, action=action, order_type=OrderType.MARKET, quantity=quantity
        )
        registry.register_order(order)
        trade = Trade(
            id=str(uuid.uuid4()),
            order_id=order.id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            entry_time=index[int(entry_bar)],
            entry_price=float(run.trades["entry_price"][i]),
            point_value=point_value,
            commission=float(run.trades["commission"][i]),
        )
        registry.register_trade(trade)
        exit_bar = int(run.trades["exit_bar"][i])
        if exit_bar >= 0:
            closed = registry.close_trade(
                trade.id, index[exit_bar], float(run.trades["exit_price"][i]), run.exit_reason_text[i]
            )
            assert closed is not None
            assert closed.pnl == float(run.trades["pnl"][i]), "q_core and TradeRegistry PnL diverged"
    return registry


def reference_decisions(
    strategy: TradingStrategy, frame: pd.DataFrame, signals: SignalArrays, open_trades: list[Trade]
) -> list[tuple[list[Signal], list[Signal]]]:
    """Produce queued decisions bar-by-bar through evaluate_bar for live parity."""
    return [evaluate_bar(strategy, frame, signals, position, open_trades) for position in range(len(frame))]
