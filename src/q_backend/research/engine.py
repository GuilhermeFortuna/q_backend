"""Synchronous research backtesting facade."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import replace
from numbers import Integral, Real
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from q_backend.backtesting.candle_kernel import engine as kernel
from q_backend.backtesting.candle_kernel import ledger_to_registry
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.exit_rules.registry import all_param_specs
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.signal_columns import BAR_INDEX, LEVEL_COLUMNS, SIGNAL_COLUMNS
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import (
    UnsupportedStrategyWorkflowError,
    get_registered_strategy,
)
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.adapter import ResearchStrategyAdapter
from q_backend.research.charting import STYLE_FIELDS, ChartIndicator
from q_backend.research.frame import _validate_prices
from q_backend.research.hooks import ticks_mode
from q_backend.research.intrabar import intrabar_error, native_tick_source
from q_backend.research.progress import BacktestProgress, backtest_progress
from q_backend.research.results import (
    BacktestResult,
    build_equity_curve,
    empty_rejected_frame,
    empty_trades_frame,
    rejected_entries_frame,
    trades_to_frame,
)
from q_backend.research.strategy import ResearchStrategy
from q_backend.research.session_blocks import run_session_blocks, session_blocks
from q_backend.research.tick_data import TickData, TickSource
from q_backend.research.tick_phase import resolve_workers
from q_backend.research.tick_store import TickStore

_INTERNAL_COLUMNS = frozenset((*SIGNAL_COLUMNS, *LEVEL_COLUMNS, BAR_INDEX))
# Session ends at which the causality check recomputes indicators, at most.
_CAUSALITY_PREFIXES = 20


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
    return tuple(
        ChartIndicator(
            spec.key,
            pane=spec.pane,
            label=spec.label,
            **{field: getattr(spec, field) for field in STYLE_FIELDS},
        )
        for spec in specs
    )


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
            "DataFrame index must be a timezone-aware DatetimeIndex. Use df.tz_localize('America/Sao_Paulo') if naive."
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
    post_exit_bars: int = 10,
    excursions: bool = True,
    force_close_at_end: bool = False,
    ticks: TickStore | None = None,
    tick_size: float | None = None,
    workers: int | Literal["auto"] = 1,
    progress: bool | None = None,
    check_causality: bool = False,
) -> BacktestResult:
    """
    Run a synchronous research backtest with deterministic Q execution semantics.

    Parameters
    ----------
    frame : pandas.DataFrame
        Historical OHLCV bars indexed by timezone-aware timestamps.
    strategy : ResearchStrategy or str
        Custom strategy instance or registered candle strategy name.
    symbol : str
        Trading symbol ticker (must match ticks symbol if ticks are provided).
    strategy_params : Mapping[str, Any], optional
        Parameter overrides for registered strategies.
    quantity : int, default 1
        Fixed order quantity per trade (positive integer).
    point_value : float, default 1.0
        Financial value per price point (e.g., 0.20 for WIN, 10.0 for WDO).
    initial_capital : float, default 100000.0
        Starting portfolio capital in account currency.
    costs : TransactionCostConfig, optional
        Per-contract and basis-point transaction fee model. None models zero cost.
    exit_params : Mapping[str, Any], optional
        Configurable stop-loss, take-profit, or trailing rules.
    day_trade : bool, default False
        If True, restricts entries to session trading hours and forces session close.
    day_trade_start_time : str, default '09:00'
        Earliest allowable entry time (HH:MM).
    day_trade_end_time : str, default '16:00'
        Latest allowable entry time (HH:MM).
    day_trade_close_time : str, default '17:00'
        Mandatory end-of-day square-off time (HH:MM).
    post_exit_bars : int, default 10
        Number of bars after exit included in post-exit excursion measurements.
    excursions : bool, default True
        Measure per-trade excursions. Disable this for parameter searches that do not use the
        excursion fields; those fields remain null and no deal data is read for measurement.
    force_close_at_end : bool, default False
        Whether to liquidate any open position on the final bar.
    ticks : TickStore, optional
        TickStore providing intrabar stop/target and priced-close confirmation. Hooks can also read
        it: a ``ticks`` parameter on ``compute_indicators``, ``entry_strategy`` or ``exit_strategy``
        receives a :class:`~q_backend.research.TickData` (``None`` without a store when it has a
        default), and a required ``ticks`` parameter makes this argument mandatory.
    tick_size : float, optional
        Instrument price increment for the Breakeven Emergency Stop. Defaults to
        ``frame.attrs['q_research']['tick_size']`` when recorded by the data loader.
    workers : int or 'auto', default 1
        Worker processes for day-trade sessions. With ``day_trade=True`` the sessions are split
        into up to ``workers`` contiguous blocks, each run in its own process, and joined in
        session order with the same result as ``workers=1``. Other runs are sequential, whatever
        the value. The strategy must be picklable from a module when ``workers > 1``; its hooks
        must depend only on their arguments and on state that resets within a session.
    progress : bool, optional
        Whether to display a progress bar on stderr.
    check_causality : bool, default False
        Before running, recompute ``compute_indicators`` on the history up to up to 20 evenly
        spaced session ends and compare it with the full run. An indicator that reads a later bar
        raises ``ValueError`` naming its column and bar. Off, the check costs nothing. Custom
        strategies only.

    Returns
    -------
    BacktestResult
        Result container with metrics, trades, equity series, and metadata.
    """
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

    worker_count = resolve_workers(workers)
    if progress is not None and not isinstance(progress, bool):
        raise TypeError(f"progress must be a bool or None, got {type(progress).__name__}")
    if not isinstance(check_causality, bool):
        raise TypeError(f"check_causality must be a bool, got {type(check_causality).__name__}")
    if not isinstance(excursions, bool):
        raise ValueError(f"excursions must be a bool, got {excursions!r}")

    if ticks is not None and ticks.symbol != symbol:
        raise ValueError(f"ticks is a store for {ticks.symbol!r}, but the backtest symbol is {symbol!r}")

    is_builtin = isinstance(strategy, str)
    if check_causality and is_builtin:
        raise ValueError(
            "check_causality applies to ResearchStrategy instances; registered strategies are causal by "
            "construction and covered by their own test suites"
        )
    validated_frame = _validate_input_frame(frame, is_builtin=is_builtin)
    timeframe = frame.attrs.get("q_research", {}).get("timeframe") if hasattr(frame, "attrs") else None
    reporter = backtest_progress(progress, " ".join(part for part in (symbol, timeframe) if part))

    # 2. Strategy instantiation
    tick_source = None if ticks is None else TickSource(ticks)
    if isinstance(strategy, ResearchStrategy):
        if strategy_params is not None:
            raise ValueError("strategy_params cannot be passed when strategy is a ResearchStrategy instance")

        trading_strategy = ResearchStrategyAdapter(
            research_strategy=strategy,
            symbol=symbol,
            exit_params=exit_params,
            tick_source=tick_source,
            progress=reporter,
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
        intrabar_factory=None if ticks is None else _NativeTickSources(ticks),
        tick_size=tick_size,
    )

    if isinstance(post_exit_bars, bool) or not 1 <= post_exit_bars <= 500:
        raise ValueError("post_exit_bars must be an integer between 1 and 500")
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
        "post_exit_bars": post_exit_bars,
        "excursions": excursions,
    }
    from q_backend.backtesting.candle_kernel import _exit_params_from_strategy

    effective_exit_params = _exit_params_from_strategy(trading_strategy)
    if float(effective_exit_params.get("breakeven_trigger_pct", 0.0)) > 0.0:
        config_dict["exit_params"] = effective_exit_params
        config_dict["tick_size"] = (
            engine.tick_size
            if engine.tick_size is not None
            else validated_frame.attrs.get("q_research", {}).get("tick_size")
        )
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
            _strategy=strategy,
            rejected_entries=empty_rejected_frame(),
            order_kinds=frozenset(getattr(trading_strategy, "order_kinds", set())),
        )

    try:
        with reporter:
            result = _execute_backtest(
                engine=engine,
                trading_strategy=trading_strategy,
                validated_frame=validated_frame,
                initial_capital=float(initial_capital),
                force_close_at_end=force_close_at_end,
                config=read_only_config,
                strategy=strategy,
                workers=worker_count,
                reporter=reporter,
                check_causality=check_causality,
                tick_source=tick_source,
            )
            reporter.finish(len(result.trades))
    except kernel.IntrabarDataError as exc:
        raise intrabar_error(exc, validated_frame, ticks) from exc
    closed = list(result._closed_trades)
    columns = [
        "mfe_points",
        "mae_points",
        "mfe_value",
        "mae_value",
        "mfe_time",
        "mae_time",
        "mfe_capture",
        "post_exit_mfe_points",
        "post_exit_mae_points",
        "post_exit_return_points",
        "post_exit_bars_observed",
        "exit_bar_rest_high",
        "exit_bar_rest_low",
        "excursion_resolution",
    ]
    if excursions and closed and not result.data.empty:
        from q_backend.backtesting.excursions import DealBar, measure_trades
        from q_backend.market_data.exogenous_context import bar_duration

        def deal_source(bar: int) -> DealBar | None:
            if ticks is None:
                return None
            start = result.data.index[bar]
            end = result.data.index[bar + 1] if bar + 1 < len(result.data) else start + bar_duration(timeframe or "M1")
            times, prices = ticks.trade_prices(start.to_pydatetime(), end.to_pydatetime())
            return DealBar(pd.to_datetime(times, unit="us", utc=True), prices, "tick")

        measure_trades(
            closed,
            result.data,
            deals=deal_source if ticks is not None else None,
            post_exit_bars=post_exit_bars,
            day_trade_close=day_trade_close_time if day_trade else None,
        )
        output = result.trades.copy()
        by_id = {
            trade.id: (
                trade.mfe_points,
                trade.mae_points,
                None if trade.mfe_points is None else trade.mfe_points * trade.quantity * trade.point_value,
                None if trade.mae_points is None else trade.mae_points * trade.quantity * trade.point_value,
                trade.mfe_time,
                trade.mae_time,
                trade.mfe_capture,
                trade.post_exit_mfe_points,
                trade.post_exit_mae_points,
                trade.post_exit_return_points,
                trade.post_exit_bars_observed,
                trade.exit_bar_rest_high,
                trade.exit_bar_rest_low,
                trade.excursion_resolution,
            )
            for trade in closed
        }
        schema = empty_trades_frame()
        arrays = {
            column: pd.array(
                [by_id.get(trade_id, (None,) * len(columns))[position] for trade_id in output["trade_id"]],
                dtype=schema[column].dtype,
            )
            for position, column in enumerate(columns)
        }
        output[columns] = pd.DataFrame(arrays, index=output.index)
        result = replace(result, trades=output, _closed_trades=tuple(closed))
    elif not excursions and len(result.trades):
        output = result.trades.copy()
        schema = empty_trades_frame()
        output[columns] = pd.DataFrame(
            {column: pd.array([None] * len(output), dtype=schema[column].dtype) for column in columns},
            index=output.index,
        )
        result = replace(result, trades=output)
    return result


def _check_causality(
    strategy: ResearchStrategy,
    frame: pd.DataFrame,
    prepared: pd.DataFrame,
    tick_source: TickSource | None,
) -> None:
    """Recompute indicators on prefixes ending at session ends, and compare them with the full run.

    An indicator that reads a later bar takes a different value on a prefix that lacks that bar,
    so the first difference names the column and the bar. A history of one session has no earlier
    session end to check.
    """
    name = type(strategy).__name__
    days = np.asarray(frame.index.tz_convert(BRASILIA_TZ).date)
    ends = np.flatnonzero(days[1:] != days[:-1])  # the last bar of each session but the final one
    if len(ends) == 0:
        return
    picks = np.unique(np.linspace(0, len(ends) - 1, min(_CAUSALITY_PREFIXES, len(ends))).round().astype(int))
    takes_ticks = ticks_mode(strategy.compute_indicators) != "none"
    for end in ends[picks]:
        stop = int(end) + 1
        kwargs: dict[str, Any] = {}
        if takes_ticks and tick_source is not None:
            kwargs["ticks"] = TickData(tick_source, frame.index, stop - 1)
        try:
            computed = strategy.compute_indicators(frame.iloc[:stop].copy(), **kwargs)
        except Exception as exc:
            raise RuntimeError(f"Error in {name}.compute_indicators during the causality check: {exc}") from exc
        for column in computed.columns:
            if column not in prepared.columns:
                continue
            matches = _same_values(computed[column], prepared[column].iloc[:stop])
            if not matches.all():
                bar = frame.index[int(np.flatnonzero(~matches)[0])]
                raise ValueError(
                    f"{name}.compute_indicators is not causal: column {column!r} at bar {bar} "
                    f"changes when the bars after it are removed (history ends {frame.index[stop - 1]})"
                )


def _same_values(left: pd.Series, right: pd.Series) -> np.ndarray:
    """Element-wise match: missing values match missing values, and floats match within rounding."""
    both_missing = left.isna().to_numpy() & right.isna().to_numpy()
    if is_numeric_dtype(left) and is_numeric_dtype(right):
        close = np.isclose(left.to_numpy(dtype=np.float64), right.to_numpy(dtype=np.float64), rtol=1e-9, atol=1e-12)
        return close | both_missing
    return (left.to_numpy() == right.to_numpy()) | both_missing


@dataclass(frozen=True)
class _NativeTickSources:
    """Builds each run's candle price source from its store; picklable for session worker processes."""

    store: TickStore

    def __call__(self, chunk: pd.DataFrame) -> Any:
        return native_tick_source(self.store, chunk)


def _execute_backtest(
    engine: BacktestEngine,
    trading_strategy: Any,
    validated_frame: pd.DataFrame,
    initial_capital: float,
    force_close_at_end: bool,
    config: Mapping[str, Any],
    strategy: Any,
    workers: int,
    reporter: BacktestProgress,
    check_causality: bool,
    tick_source: TickSource | None,
) -> BacktestResult:
    # Prepare once, then reuse explicitly for execution and result data.
    augmented_data = augment_indicator_frame(trading_strategy, validated_frame)
    if check_causality:
        _check_causality(strategy, validated_frame, augmented_data, tick_source)
    blocks = session_blocks(augmented_data.index, workers) if engine.day_trade else None
    if blocks is None:
        registry, run = engine.run_prepared_detailed(augmented_data, force_close_at_end=force_close_at_end)
    else:
        run = run_session_blocks(
            engine,
            augmented_data,
            blocks,
            force_close_at_end=force_close_at_end,
            workers=workers,
            strategy_name=type(strategy).__name__,
            strategy_module=type(strategy).__module__,
            on_block_done=lambda bars, timestamp: reporter.bar(bars, timestamp),
        )
        registry = ledger_to_registry(
            run,
            augmented_data.index,
            symbol=engine.strategy.symbol,
            point_value=engine.point_values.get(engine.strategy.symbol, 1.0),
        )

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
        _strategy=strategy,
        rejected_entries=rejected_entries_frame(run, augmented_data.index),
        order_kinds=frozenset(getattr(trading_strategy, "order_kinds", set())),
    )
