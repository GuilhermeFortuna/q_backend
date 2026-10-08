"""Synchronous research backtesting facade."""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Integral, Real
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.exit_rules.registry import all_param_specs
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.signal_columns import BAR_INDEX, SIGNAL_COLUMNS
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import (
    UnsupportedStrategyWorkflowError,
    get_registered_strategy,
)
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.adapter import ResearchStrategyAdapter
from q_backend.research.charting import ChartIndicator
from q_backend.research.frame import _validate_prices
from q_backend.research.results import BacktestResult, build_equity_curve, trades_to_frame
from q_backend.research.strategy import ResearchStrategy

_INTERNAL_COLUMNS = frozenset((*SIGNAL_COLUMNS, BAR_INDEX))


def _is_json_scalar(val: Any) -> bool:
    if val is None or isinstance(val, (bool, str, int, np.integer)):
        return True
    if isinstance(val, (float, np.floating)):
        return math.isfinite(val)
    return False


def _normalize_scalar(val: Any) -> Any:
    if isinstance(val, bool) or val is None:
        return val
    if isinstance(val, (int, np.integer)):
        return int(val)
    if isinstance(val, (float, np.floating)):
        return float(val)
    return val


def _extract_strategy_params(strategy: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    attr_names: list[str] = list(vars(strategy).keys()) if hasattr(strategy, "__dict__") else []
    for name in dir(strategy):
        if name not in attr_names:
            attr_names.append(name)

    for name in attr_names:
        if name.startswith("_"):
            continue
        try:
            val = getattr(strategy, name)
        except Exception:  # noqa: BLE001
            continue
        if callable(val):
            continue
        if _is_json_scalar(val):
            params[name] = _normalize_scalar(val)
    return params


def _chart_indicators(trading_strategy: TradingStrategy) -> tuple[ChartIndicator, ...]:
    # The base TradingStrategy hook returns None; treat that as no declared series.
    specs = trading_strategy.get_chart_indicators() or ()
    return tuple(ChartIndicator(spec.key, pane=spec.pane, label=spec.label, color=spec.color) for spec in specs)


def _validate_input_frame(frame: pd.DataFrame, is_builtin: bool) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"Expected DataFrame, got {type(frame).__name__}")

    if frame.columns.has_duplicates:
        raise ValueError("Input DataFrame contains duplicate column names")

    for column in _INTERNAL_COLUMNS:
        if column in frame.columns:
            raise ValueError(f"Input DataFrame contains reserved column {column!r}")

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(
            "DataFrame index must be a timezone-aware DatetimeIndex. "
            "Use df.tz_localize('America/Sao_Paulo') if naive."
        )

    if frame.index.tz is None:
        raise ValueError(
            "DataFrame index has no timezone. Expected timezone-aware DatetimeIndex. "
            "Use df.tz_localize('America/Sao_Paulo') to set timezone."
        )

    if not frame.index.is_monotonic_increasing:
        raise ValueError("DataFrame index must be strictly ascending")

    if frame.index.has_duplicates:
        raise ValueError("DataFrame index contains duplicate timestamps")

    # If empty, check required columns exist
    for col in ("open", "high", "low", "close"):
        if col not in frame.columns:
            raise ValueError(f"Missing required OHLC column: {col}")

    if not frame.empty:
        _validate_prices(frame)

    if is_builtin:
        # Built-in strategies check volume requirements
        if "tick_volume" not in frame.columns and "volume" not in frame.columns:
            raise ValueError("Built-in strategy requires volume / tick_volume column")
    else:
        # Custom strategies: validate volume columns if present
        for col in ("tick_volume", "real_volume", "volume"):
            if col in frame.columns and not frame.empty:
                vals = frame[col].to_numpy(dtype=np.float64, copy=False)
                if not np.all(np.isfinite(vals) | np.isnan(vals)):
                    raise ValueError(f"Non-finite values in volume column {col!r}")
                if np.any(vals[np.isfinite(vals)] < 0):
                    raise ValueError(f"Negative values in volume column {col!r}")

    # Convert aware index to America/Sao_Paulo
    frame_converted = frame.copy()
    if frame_converted.index.tz != BRASILIA_TZ:
        frame_converted.index = frame_converted.index.tz_convert(BRASILIA_TZ)
    frame_converted.index.name = "time"
    return frame_converted


def _validate_session_times(start: str, end: str, close: str) -> None:
    from q_backend.backtesting.candle_kernel import parse_day_trade_times

    start_us, end_us, close_us = parse_day_trade_times(start, end, close)
    if not (start_us <= end_us <= close_us):
        raise ValueError(
            f"Invalid day-trade session order: start ({start}) <= end ({end}) <= close ({close}) required."
        )


def _validate_exit_params(params: Mapping[str, Any]) -> None:
    specs = {spec.name: spec for spec in all_param_specs()}
    for name, value in params.items():
        if name not in specs:
            raise ValueError(f"Unknown exit_param: {name!r}")
        spec = specs[name]
        if spec.type == "categorical":
            if value not in (spec.choices or []):
                raise ValueError(f"{name} must be one of {spec.choices!r}")
            continue
        expected_type = Integral if spec.type == "int" else Real
        if isinstance(value, bool) or not isinstance(value, expected_type) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite {spec.type}, got {value!r}")
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{name} must be >= {spec.min}, got {value!r}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{name} must be <= {spec.max}, got {value!r}")


def backtest(
    frame: pd.DataFrame,
    *,
    strategy: ResearchStrategy | str,
    symbol: str,
    strategy_params: Mapping[str, Any] | None = None,
    quantity: int = 1,
    point_value: float = 1.0,
    initial_capital: float = 100000.0,
    costs: TransactionCostConfig | None = None,
    exit_params: Mapping[str, Any] | None = None,
    day_trade: bool = False,
    day_trade_start_time: str = "09:00",
    day_trade_end_time: str = "16:00",
    day_trade_close_time: str = "17:00",
    force_close_at_end: bool = False,
) -> BacktestResult:
    """Run a synchronous research backtest with Q engine execution semantics."""
    # 1. Validate scalar arguments
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError(f"quantity must be a positive integer, got {quantity!r}")

    if (
        not isinstance(point_value, (int, float))
        or isinstance(point_value, bool)
        or not math.isfinite(point_value)
        or point_value <= 0
    ):
        raise ValueError(f"point_value must be a finite positive number, got {point_value!r}")

    if (
        not isinstance(initial_capital, (int, float))
        or isinstance(initial_capital, bool)
        or not math.isfinite(initial_capital)
        or initial_capital <= 0
    ):
        raise ValueError(f"initial_capital must be a finite positive number, got {initial_capital!r}")

    if costs is not None:
        if not isinstance(costs, TransactionCostConfig):
            raise TypeError(f"costs must be TransactionCostConfig or None, got {type(costs).__name__}")
        if not math.isfinite(costs.cost_per_contract) or costs.cost_per_contract < 0:
            raise ValueError("cost_per_contract must be finite and >= 0")
        if not math.isfinite(costs.cost_bps) or costs.cost_bps < 0:
            raise ValueError("cost_bps must be finite and >= 0")

    if day_trade:
        _validate_session_times(day_trade_start_time, day_trade_end_time, day_trade_close_time)

    if exit_params is not None:
        _validate_exit_params(exit_params)

    is_builtin = isinstance(strategy, str)
    validated_frame = _validate_input_frame(frame, is_builtin=is_builtin)

    # 2. Strategy instantiation
    if isinstance(strategy, ResearchStrategy):
        if strategy_params is not None:
            raise ValueError("strategy_params cannot be passed when strategy is a ResearchStrategy instance")

        trading_strategy = ResearchStrategyAdapter(
            research_strategy=strategy,
            symbol=symbol,
            exit_params=exit_params,
        )
    elif isinstance(strategy, str):
        # Built-in strategy
        try:
            reg_entry = get_registered_strategy(strategy)
        except KeyError:
            raise ValueError(f"Unknown strategy: {strategy!r}") from None

        if reg_entry.info.engine != "candle":
            raise ValueError(
                f"Strategy {strategy!r} has engine {reg_entry.info.engine!r}; only 'candle' is supported in backtest()"
            )

        if "research_only" in reg_entry.info.capabilities or "ml_entry_filter" in reg_entry.info.capabilities:
            raise UnsupportedStrategyWorkflowError(
                f"Strategy {strategy!r} requires service/ML hydration and is not supported in local backtest()"
            )

        # Check for conflicting params between strategy_params and exit_params
        strat_p = dict(strategy_params or {})
        exit_p = dict(exit_params or {})
        overlap = set(strat_p.keys()) & set(exit_p.keys())
        if overlap:
            raise ValueError(f"Conflicting parameters in both strategy_params and exit_params: {overlap}")

        # Check unknown strategy_params
        known_strategy_params = {p.name for p in reg_entry.info.params}
        for k in strat_p:
            if k not in known_strategy_params:
                raise ValueError(f"Unknown strategy parameter: {k!r} for strategy {strategy!r}")

        merged_params = dict(strat_p)
        merged_params.update(exit_p)
        exit_names = {spec.name for spec in all_param_specs()}
        _validate_exit_params({name: value for name, value in merged_params.items() if name in exit_names})

        trading_strategy = build_strategy(strategy, merged_params, symbol=symbol)
    else:
        raise TypeError(f"strategy must be a ResearchStrategy instance or str, got {type(strategy).__name__}")

    # 3. Position Sizer
    sizer = FixedQuantitySizer(quantity=float(quantity), scale_by_signal_strength=False)

    # 4. Engine Run
    engine = BacktestEngine(
        strategy=trading_strategy,
        sizer=sizer,
        initial_capital=float(initial_capital),
        point_values={symbol: float(point_value)},
        day_trade=day_trade,
        day_trade_start_time=day_trade_start_time,
        day_trade_end_time=day_trade_end_time,
        day_trade_close_time=day_trade_close_time,
        costs=costs,
    )

    timeframe = frame.attrs.get("q_research", {}).get("timeframe") if hasattr(frame, "attrs") else None
    if isinstance(strategy, str):
        strat_name = strategy
        strat_params = dict(strategy_params or {})
    else:
        strat_name = type(strategy).__name__
        strat_params = _extract_strategy_params(strategy)

    config_dict: dict[str, Any] = {
        "symbol": symbol,
        "strategy": strat_name,
        "strategy_params": strat_params,
        "quantity": quantity,
        "point_value": float(point_value),
        "initial_capital": float(initial_capital),
        "costs": costs,
        "exit_params": dict(exit_params) if exit_params is not None else None,
        "day_trade": day_trade,
        "day_trade_start_time": day_trade_start_time,
        "day_trade_end_time": day_trade_end_time,
        "day_trade_close_time": day_trade_close_time,
        "force_close_at_end": force_close_at_end,
        "timeframe": timeframe,
    }
    read_only_config = MappingProxyType(config_dict)

    if validated_frame.empty:
        # There are no bars to prepare; user hooks need not handle empty history.
        clean_data = validated_frame.copy()
        registry = engine.run(validated_frame, force_close_at_end=force_close_at_end)
        metrics = registry.get_performance_metrics(initial_capital=float(initial_capital))
        trades = trades_to_frame(registry)
        equity = build_equity_curve(registry, clean_data.index, float(initial_capital))
        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity=equity,
            data=clean_data,
            indicators=_chart_indicators(trading_strategy),
            config=read_only_config,
            _closed_trades=tuple(registry.get_closed_trades()),
        )

    return _execute_backtest(
        engine=engine,
        trading_strategy=trading_strategy,
        validated_frame=validated_frame,
        initial_capital=float(initial_capital),
        force_close_at_end=force_close_at_end,
        config=read_only_config,
    )


def _execute_backtest(
    engine: BacktestEngine,
    trading_strategy: Any,
    validated_frame: pd.DataFrame,
    initial_capital: float,
    force_close_at_end: bool,
    config: Mapping[str, Any],
) -> BacktestResult:
    # If trading_strategy is ResearchStrategyAdapter, cache its augmented data
    # so subsequent call inside engine._run_single_chunk reuses it.
    augmented_data: pd.DataFrame
    if isinstance(trading_strategy, ResearchStrategyAdapter):
        # Augment once
        augmented_data = augment_indicator_frame(trading_strategy, validated_frame)
        # Store cached result so adapter.compute_indicators returns it directly
        cached_result = augmented_data.copy()

        original_compute = trading_strategy.compute_indicators
        call_count = 0

        def cached_compute(data: pd.DataFrame) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return cached_result
            return original_compute(data)

        trading_strategy.compute_indicators = cached_compute  # type: ignore[method-assign]
        try:
            registry = engine.run(validated_frame, force_close_at_end=force_close_at_end)
        finally:
            trading_strategy.compute_indicators = original_compute  # type: ignore[method-assign]
    else:
        # Built-in strategy
        augmented_data = augment_indicator_frame(trading_strategy, validated_frame)
        registry = engine.run(validated_frame, force_close_at_end=force_close_at_end)

    # Clean data (omit internal columns)
    clean_data = augmented_data.drop(columns=[col for col in _INTERNAL_COLUMNS if col in augmented_data.columns])

    metrics = registry.get_performance_metrics(initial_capital=initial_capital)
    trades = trades_to_frame(registry)
    equity = build_equity_curve(registry, clean_data.index, initial_capital)
    return BacktestResult(
        metrics=metrics,
        trades=trades,
        equity=equity,
        data=clean_data,
        indicators=_chart_indicators(trading_strategy),
        config=config,
        _closed_trades=tuple(registry.get_closed_trades()),
    )
