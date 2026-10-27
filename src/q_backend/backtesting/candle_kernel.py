"""Boundary bridge from the candle engine to q_core's candle kernel."""

from __future__ import annotations

import datetime
import inspect
import re
import uuid
from collections.abc import Callable
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
from q_backend.market_data.timezone import BRASILIA_TZ

REQUIRED_ENGINE_FUNCTIONS: Final = ("run_candle", "DecisionStep", "size_entry", "required_columns")
REQUIRED_ENGINE_CAPABILITIES: Final = ("PROTECTIVE_ORDERS", "RESTING_ORDERS", "SESSION_TICK_SOURCE")
# Entry kinds in the q_core ledger, by code: an open fill, a priced entry, and resting stop and limit orders.
ENTRY_KIND_ORDER: Final = ("market", "priced", "stop", "limit")
_RESTING_KINDS: Final = (2, 3)
REQUIRED_RUN_CANDLE_PARAMETERS: Final = (
    "stop_price",
    "target_price",
    "entry_price",
    "intrabar",
    "strategy_callback",
    "snapshot_levels",
    "entry_fill_callback",
)


def check_engine(module: Any) -> None:
    """Raise a startup error if the installed q_core lacks the candle API this bridge uses."""
    target = getattr(module, "engine", module)
    version_fn = getattr(module, "version", None)
    version = version_fn() if callable(version_fn) else getattr(module, "__version__", "unknown")
    missing = [name for name in REQUIRED_ENGINE_FUNCTIONS if not hasattr(target, name)]
    missing += [name for name in REQUIRED_ENGINE_CAPABILITIES if getattr(target, name, None) is not True]
    if missing:
        raise ImportError(f"q_core {version} is missing required candle kernel functions: {', '.join(missing)}")
    parameters = inspect.signature(target.run_candle).parameters
    absent = [name for name in REQUIRED_RUN_CANDLE_PARAMETERS if name not in parameters]
    if absent:
        raise ImportError(f"q_core {version} run_candle lacks required parameters: {', '.join(absent)}")


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
    entry_reason: list[str | None]
    rejected: dict[str, np.ndarray]


def _column(frame: pd.DataFrame, name: str) -> np.ndarray | None:
    if name not in frame:
        return None
    return np.ascontiguousarray(pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan))


def _exit_params(source: dict[str, object]) -> dict[str, object]:
    from q_backend.backtesting.exit_rules.registry import all_param_specs

    params = {spec.name: source[spec.name] for spec in all_param_specs() if spec.name in source}
    if float(params.get("breakeven_trigger_pct", 0.0)) > 0.0:
        emergency = next(spec for spec in all_param_specs() if spec.name == "breakeven_emergency_stop_ticks")
        params.setdefault(emergency.name, emergency.default)
    return params


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
    intrabar: Callable[[int], tuple[np.ndarray, np.ndarray]] | None = None,
    tick_size: float | None = None,
) -> ChunkRun:
    """Run one already-augmented candle chunk through q_core without row objects.

    ``intrabar`` supplies a candle's trade prices when the kernel must confirm a stop, target
    or custom exit inside it. Runtime decisions come from ``strategy.runtime_callback``.
    """
    source_params = getattr(getattr(strategy, "exit_strategy", None), "params", strategy.parameters)
    exit_params = _exit_params(source_params)
    if float(exit_params.get("breakeven_trigger_pct", 0.0)) > 0.0:
        if getattr(engine, "BREAKEVEN_INTRABAR", False) is not True:
            raise ImportError(
                "Installed q_core lacks BREAKEVEN_INTRABAR; install the release providing intrabar breakeven stops."
            )
        if tick_size is not None:
            exit_params["tick_size"] = tick_size
    columns = {
        name: values for name in engine.required_columns(exit_params) if (values := _column(chunk, name)) is not None
    }
    tradable = None if trade_start is None else np.ascontiguousarray(~(chunk.index < trade_start), dtype=bool)
    runtime_factory = getattr(strategy, "runtime_callback", None)
    runtime = runtime_factory(chunk) if runtime_factory is not None else None
    level_args: dict[str, object] = {}
    if runtime is None:
        decided_stop = signals.stop_price
        decided_target = signals.target_price
        decided_entry = signals.entry_price
        if decided_stop is not None:
            level_args["stop_price"] = np.ascontiguousarray(decided_stop)
            level_args["target_price"] = np.ascontiguousarray(decided_target)
        if decided_entry is not None:
            level_args["entry_price"] = np.ascontiguousarray(decided_entry)
    else:
        decided_stop = runtime.stop_price
        decided_target = runtime.target_price
        decided_entry = runtime.entry_price
        level_args = {"strategy_callback": runtime.strategy, "entry_fill_callback": runtime.entry_filled}
    if intrabar is not None:
        level_args["intrabar"] = intrabar
    if runtime is not None:
        level_args["snapshot_levels"] = True
    try:
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
            **level_args,
        )
    except ValueError as exc:
        msg = str(exc)
        match = re.search(
            r"entry_price\[(?P<bar>\d+)\]:\s*(?P<side>\w+)\s*price\s*(?P<price>[^\s]+)\s*is outside the bar range \[(?P<low>[^,]+),\s*(?P<high>[^\]]+)\]",
            msg,
        )
        if match:
            bar_idx = int(match.group("bar"))
            bar_time = chunk.index[bar_idx]
            strat_name = type(getattr(strategy, "research_strategy", strategy)).__name__
            price = match.group("price")
            low = match.group("low")
            high = match.group("high")
            raise ValueError(
                f"{strat_name} entry price {price} at bar {bar_idx} ({bar_time}) is outside the bar range [{low}, {high}]"
            ) from exc
        exit_match = re.search(r"exit_price\[(?P<bar>\d+)\]: must lie within the bar's low and high", msg)
        if exit_match and runtime is not None:
            bar_idx = int(exit_match.group("bar"))
            bar_time = chunk.index[bar_idx]
            price = runtime.exit_price[bar_idx]
            low = _column(chunk, "low")[bar_idx]
            high = _column(chunk, "high")[bar_idx]
            strat_name = type(getattr(strategy, "research_strategy", strategy)).__name__
            raise ValueError(
                f"{strat_name} exit price {price:g} at bar {bar_idx} ({bar_time}) is outside the bar range "
                f"[{low:g}, {high:g}]"
            ) from exc
        raise
    entry_bar = np.asarray(result["entry_bar"], dtype=np.int64)
    entry_kind = np.asarray(result["entry_kind"], dtype=np.int64)
    exit_bar = np.asarray(result["exit_bar"], dtype=np.int64)
    # Only accepted decisions replace a working order; hook requests may be gated out.
    placed_bars = np.flatnonzero(result["entry"]) if runtime is not None else np.empty(0, dtype=np.int64)

    def decided_bar(i: int) -> int:
        b = int(entry_bar[i])
        if entry_kind[i] in _RESTING_KINDS:
            before = int(np.searchsorted(placed_bars, b)) - 1
            return int(placed_bars[before]) if before >= 0 else -1
        is_priced = (
            decided_entry is not None
            and not np.isnan(decided_entry[b])
            and (decided_entry[b] == result["entry_price"][i] or np.isclose(decided_entry[b], result["entry_price"][i]))
        )
        return b if is_priced else b - 1

    def levels_of(decided: np.ndarray | None) -> np.ndarray:
        out = np.full(len(entry_bar), np.nan)
        if decided is not None:
            for i in range(len(entry_bar)):
                bar = decided_bar(i)
                if bar >= 0:
                    out[i] = decided[bar]
        return out

    def close_reason_at(fill_bar: int) -> str | None:
        # A plain close decided on the bar before the fill exits there, and blocks a priced close on the fill bar,
        # which then finds no position. Otherwise the exit is a priced close filled on its own bar.
        if fill_bar >= 1 and runtime.fill_close_requested[fill_bar - 1]:
            return runtime.fill_close_reason[fill_bar - 1]
        if fill_bar >= 1 and runtime.close_requested[fill_bar - 1] and np.isnan(runtime.exit_price[fill_bar - 1]):
            return runtime.close_reason[fill_bar - 1]
        if runtime.close_requested[fill_bar] and not np.isnan(runtime.exit_price[fill_bar]):
            return runtime.close_reason[fill_bar]
        return None

    exit_reason_text = list(result["exit_reason_text"])
    entry_reason: list[str | None] = [None] * len(entry_bar)
    if runtime is not None:
        for i in range(len(entry_bar)):
            bar = decided_bar(i)
            if bar >= 0:
                entry_reason[i] = runtime.entry_reason[bar]
        # Only a SIGNAL exit takes the close reason; stop, target and other engine exits keep their label.
        for i, text in enumerate(exit_reason_text):
            if text == "SIGNAL" and exit_bar[i] >= 0:
                reason = close_reason_at(int(exit_bar[i]))
                if reason is not None:
                    exit_reason_text[i] = reason

    trades = {
        name: np.asarray(result[name])
        for name in (
            "entry_bar",
            "exit_bar",
            "side",
            "quantity",
            "entry_price",
            "exit_price",
            "commission",
            "pnl",
            "exit_time_us",
            "entry_time_us",
        )
    }
    trades["entry_kind"] = entry_kind
    trades["stop_loss"] = levels_of(decided_stop)
    trades["take_profit"] = levels_of(decided_target)
    trades["final_stop_loss"] = np.asarray(result["final_stop_price"], dtype=np.float64)
    trades["final_take_profit"] = np.asarray(result["final_target_price"], dtype=np.float64)
    rejected = {
        name: np.asarray(result[f"rejected_{name}"])
        for name in ("bar", "side", "fill_price", "stop_price", "target_price", "reason")
    }
    return ChunkRun(trades, exit_reason_text, entry_reason, rejected)


def merge_chunk_runs(runs: list[ChunkRun], starts: list[int]) -> ChunkRun:
    """Join kernel runs of consecutive blocks of one chunk, in order.

    ``starts[i]`` is the chunk position of block ``i``'s first bar. Bar indices in each run are
    block-relative, so they move by that offset; a negative index (no exit yet) is left as it is.
    """

    def join(name: str, offsets: bool, parts: list[np.ndarray]) -> np.ndarray:
        arrays = [np.where(part >= 0, part + start, part) if offsets else part for part, start in zip(parts, starts)]
        return np.concatenate(arrays)

    trades = {
        name: join(name, name in ("entry_bar", "exit_bar"), [run.trades[name] for run in runs])
        for name in runs[0].trades
    }
    rejected = {name: join(name, name == "bar", [run.rejected[name] for run in runs]) for name in runs[0].rejected}
    exit_reason_text = [text for run in runs for text in run.exit_reason_text]
    entry_reason = [reason for run in runs for reason in run.entry_reason]
    return ChunkRun(trades, exit_reason_text, entry_reason, rejected)


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
            entry_bar=int(entry_bar),
            entry_price=float(run.trades["entry_price"][i]),
            point_value=point_value,
            commission=float(run.trades["commission"][i]),
            stop_loss=_level(run.trades["stop_loss"][i]),
            take_profit=_level(run.trades["take_profit"][i]),
            order_kind=ENTRY_KIND_ORDER[int(run.trades["entry_kind"][i])],
            entry_reason=run.entry_reason[i],
            final_stop_loss=_level(run.trades["final_stop_loss"][i]),
            final_take_profit=_level(run.trades["final_take_profit"][i]),
        )
        entry_tick_us = int(run.trades["entry_time_us"][i])
        if entry_tick_us >= 0:
            trade.entry_tick_time = pd.Timestamp(entry_tick_us, unit="us").tz_localize(BRASILIA_TZ)
        registry.register_trade(trade)
        exit_bar = int(run.trades["exit_bar"][i])
        if exit_bar >= 0:
            closed = registry.close_trade(
                trade.id, index[exit_bar], float(run.trades["exit_price"][i]), run.exit_reason_text[i]
            )
            assert closed is not None
            closed.exit_bar = exit_bar
            assert closed.pnl == float(run.trades["pnl"][i]), "q_core and TradeRegistry PnL diverged"
            tick_us = int(run.trades["exit_time_us"][i])
            if tick_us >= 0:
                closed.exit_tick_time = pd.Timestamp(tick_us, unit="us").tz_localize(BRASILIA_TZ)
    return registry


def _level(value: float) -> float | None:
    return None if np.isnan(value) else float(value)


def reference_decisions(
    strategy: TradingStrategy, frame: pd.DataFrame, signals: SignalArrays, open_trades: list[Trade]
) -> list[tuple[list[Signal], list[Signal]]]:
    """Produce queued decisions bar-by-bar through evaluate_bar for live parity."""
    return [evaluate_bar(strategy, frame, signals, position, open_trades) for position in range(len(frame))]
