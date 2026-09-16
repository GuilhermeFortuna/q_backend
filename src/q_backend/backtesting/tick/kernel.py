"""Bridge module connecting q_backend tick simulation and chart helpers to q_core.engine.

This module is a bridge to q_core and contains no compiled functions.
"""

from __future__ import annotations

from typing import Any, Final

import numpy as np
import q_core
from q_backend.backtesting.position_sizing import PositionSizingConfig
from q_backend.backtesting.tick.orders import (
    SIZING_FIXED_QUANTITY,
    SIZING_FIXED_SAFETY_MARGIN,
)
from q_backend.backtesting.tick.strategy import TickArrays

REQUIRED_TICK_FUNCTIONS: Final[tuple[str, ...]] = (
    "tick_simulate",
    "tick_day_bounds",
    "tick_bars",
    "resolve_bar_ms",
    "sample_at_bar_ends",
)


def check_tick_engine(module: Any) -> None:
    """Raise ImportError naming every missing REQUIRED_TICK_FUNCTIONS entry and module.version()."""
    target = getattr(module, "engine", module)
    ver_fn = getattr(module, "version", None)
    if callable(ver_fn):
        version = ver_fn()
    else:
        version = getattr(module, "__version__", "unknown")

    missing = [name for name in REQUIRED_TICK_FUNCTIONS if not hasattr(target, name)]
    if missing:
        raise ImportError(f"q_core {version} is missing required tick functions: {', '.join(missing)}")


# Validate installed q_core at import time
check_tick_engine(q_core)

engine: Any = getattr(q_core, "engine", q_core)


def _sizing_config_to_dict(sizing: PositionSizingConfig | dict[str, Any]) -> dict[str, Any]:
    if isinstance(sizing, dict):
        return sizing
    if sizing.type == "fixed_quantity":
        return {"type": "fixed_quantity", "quantity": float(sizing.quantity)}
    if sizing.type == "fixed_safety_margin":
        max_c = int(sizing.max_contracts) if sizing.max_contracts is not None else 0
        return {
            "type": "fixed_safety_margin",
            "safety_margin_per_contract": float(sizing.safety_margin_per_contract),
            "min_contracts": int(sizing.min_contracts),
            "max_contracts": max_c,
        }
    raise ValueError(f"tick engine does not support position sizing type '{sizing.type}'")


def simulate_config(
    bid: np.ndarray,
    ask: np.ndarray,
    direction: np.ndarray,
    sl_points: np.ndarray,
    tp_points: np.ndarray,
    initial_capital: float,
    point_value: float,
    sizing: PositionSizingConfig | dict[str, Any],
) -> dict[str, Any]:
    """
    Run tick simulation on q_core using a position sizing config.

    Returns exact-length ledger arrays and final capital:
      entry_idx, exit_idx, entry_price, exit_price, direction, quantity,
      exit_reason, final_capital.
    """
    sizing_dict = _sizing_config_to_dict(sizing)
    return engine.tick_simulate(
        bid=np.ascontiguousarray(bid, dtype=np.float64),
        ask=np.ascontiguousarray(ask, dtype=np.float64),
        direction=np.ascontiguousarray(direction, dtype=np.int8),
        sl_points=np.ascontiguousarray(sl_points, dtype=np.float64),
        tp_points=np.ascontiguousarray(tp_points, dtype=np.float64),
        initial_capital=float(initial_capital),
        point_value=float(point_value),
        sizing=sizing_dict,
    )


def simulate(
    bid: np.ndarray,
    ask: np.ndarray,
    direction: np.ndarray,
    sl_points: np.ndarray,
    tp_points: np.ndarray,
    initial_capital: float,
    point_value: float,
    sizing_mode: int,
    sizing_a: float,
    sizing_b: float,
    sizing_c: float,
) -> tuple[np.ndarray, ...]:
    """
    Single-position intrabar simulation bridge.

    Returns parallel arrays (one row per closed trade) plus trailing metadata:
      entry_idx, exit_idx, entry_price, exit_price, direction (+1/-1),
      quantity, exit_reason (ExitReason int codes), trade_count, final_capital.
    Arrays are allocated at length n and zero-filled past trade_count.
    """
    if sizing_mode == SIZING_FIXED_QUANTITY:
        sizing_dict = {"type": "fixed_quantity", "quantity": float(sizing_a)}
    elif sizing_mode == SIZING_FIXED_SAFETY_MARGIN:
        max_c = int(sizing_c) if sizing_c > 0.0 else 0
        sizing_dict = {
            "type": "fixed_safety_margin",
            "safety_margin_per_contract": float(sizing_a),
            "min_contracts": int(sizing_b),
            "max_contracts": max_c,
        }
    else:
        raise ValueError(f"unknown sizing mode: {sizing_mode}")

    ledger = engine.tick_simulate(
        bid=np.ascontiguousarray(bid, dtype=np.float64),
        ask=np.ascontiguousarray(ask, dtype=np.float64),
        direction=np.ascontiguousarray(direction, dtype=np.int8),
        sl_points=np.ascontiguousarray(sl_points, dtype=np.float64),
        tp_points=np.ascontiguousarray(tp_points, dtype=np.float64),
        initial_capital=float(initial_capital),
        point_value=float(point_value),
        sizing=sizing_dict,
    )

    n = len(bid)
    trade_count = len(ledger["entry_idx"])

    entry_idx = np.zeros(n, dtype=np.int64)
    exit_idx = np.zeros(n, dtype=np.int64)
    entry_price = np.zeros(n, dtype=np.float64)
    exit_price = np.zeros(n, dtype=np.float64)
    trade_direction = np.zeros(n, dtype=np.int8)
    quantity = np.zeros(n, dtype=np.float64)
    exit_reason = np.zeros(n, dtype=np.int32)

    if trade_count > 0:
        entry_idx[:trade_count] = ledger["entry_idx"]
        exit_idx[:trade_count] = ledger["exit_idx"]
        entry_price[:trade_count] = ledger["entry_price"]
        exit_price[:trade_count] = ledger["exit_price"]
        trade_direction[:trade_count] = ledger["direction"]
        quantity[:trade_count] = ledger["quantity"]
        exit_reason[:trade_count] = ledger["exit_reason"]

    return (
        entry_idx,
        exit_idx,
        entry_price,
        exit_price,
        trade_direction,
        quantity,
        exit_reason,
        trade_count,
        float(ledger["final_capital"]),
    )


def day_bounds(time_msc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Find start and end indices for contiguous UTC days in a tick stream."""
    starts, ends = engine.tick_day_bounds(np.ascontiguousarray(time_msc, dtype=np.int64))
    return starts, ends


def bars(ticks: TickArrays, bar_ms: int) -> dict[str, np.ndarray]:
    """Resample ticks into OHLCV bars using q_core."""
    return engine.tick_bars(
        time_msc=np.ascontiguousarray(ticks.time_msc, dtype=np.int64),
        bid=np.ascontiguousarray(ticks.bid, dtype=np.float64),
        ask=np.ascontiguousarray(ticks.ask, dtype=np.float64),
        last=np.ascontiguousarray(ticks.last, dtype=np.float64),
        volume=np.ascontiguousarray(ticks.volume, dtype=np.float64),
        bar_ms=int(bar_ms),
    )


def resolve_bar_ms(base_bar_ms: int, span_msc: int) -> int:
    """Resolve display bar millisecond interval with adaptive doubling."""
    return int(engine.resolve_bar_ms(int(base_bar_ms), int(span_msc)))


def sample_at_bar_ends(series: np.ndarray, tick_end: np.ndarray) -> np.ndarray:
    """Sample indicator values at bar closes using tick_end indices from bars()."""
    return engine.sample_at_bar_ends(
        np.ascontiguousarray(series, dtype=np.float64),
        np.ascontiguousarray(tick_end, dtype=np.int64),
    )
