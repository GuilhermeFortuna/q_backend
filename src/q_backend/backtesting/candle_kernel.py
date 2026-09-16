"""Boundary bridge from the candle engine to q_core's candle kernel."""

from __future__ import annotations

import datetime
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
