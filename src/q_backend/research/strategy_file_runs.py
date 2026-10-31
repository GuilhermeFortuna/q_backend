"""Run a frozen strategy version through the research library's ``backtest()``.

The worker builds the version's class from its frozen source, selects provider candles or candles
built from a synchronized tick store, and passes ticks only when explicitly requested.
The result is returned as the import request that script runs are stored from, so a stack run
and a script run of the same class, bars and parameters produce the same trades and metrics.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from q_backend.api.schemas.backtest import BacktestImportRequest
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.data import MT5_SOURCE
from q_backend.research.engine import backtest
from q_backend.research.errors import TickDataRequiredError
from q_backend.research.frame import (
    attach_metadata,
    drop_forming_bar,
    filter_index_range,
    normalize_timeframe,
    ohlcv_models_to_frame,
    validate_bars_frame,
)
from q_backend.research.hooks import ticks_mode
from q_backend.research.params import coerce_params, declared_from_wire
from q_backend.research.publishing import build_import_request
from q_backend.research.strategy_files import StrategyBundle, load_bundle, validate_bundle
from q_backend.research.tick_store import TickStore

PhaseReporter = Callable[[str, str], None]


@dataclass(frozen=True)
class FrozenStrategy:
    """What a run needs from a version: its id, its frozen bundle, and its declared parameter specs."""

    strategy_id: str
    bundle: StrategyBundle
    param_specs: list[dict[str, Any]]


@dataclass(frozen=True)
class StrategyRunResult:
    """The import request to store for the run, and whether tick data was used."""

    request: BacktestImportRequest
    ticks_used: bool
    order_kinds: list[str] = field(default_factory=list)


def run_strategy(
    frozen: FrozenStrategy,
    *,
    bundle_root: Path,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    strategy_params: Mapping[str, Any],
    quantity: int,
    point_value: float,
    initial_capital: float,
    costs: TransactionCostConfig | None,
    exit_params: Mapping[str, Any],
    day_trade: bool,
    day_trade_start_time: str,
    day_trade_end_time: str,
    day_trade_close_time: str,
    post_exit_bars: int,
    market_data_service: Any,
    checkpoint: Callable[[], None],
    on_phase: PhaseReporter,
    research_data_mode: Literal["auto", "bars", "ticks"] = "auto",
) -> StrategyRunResult:
    """Execute the frozen strategy on bars from ``start`` to ``end`` and return its stored form.

    Raises ``ValueError`` for an empty range, :class:`StrategyFileError` when the frozen source
    cannot be loaded, and the research library's errors (including the causality check) otherwise.
    """
    checkpoint()
    on_phase("preparing", "Preparing strategy and indicators")
    strategy_class = load_bundle(frozen.bundle, bundle_root)
    declared = tuple(declared_from_wire(spec) for spec in frozen.param_specs)
    params = coerce_params(declared, strategy_params)
    instance = strategy_class(**params)
    _require_decision_timeframe(strategy_class, timeframe)
    if research_data_mode not in ("auto", "bars", "ticks"):
        raise ValueError("research_data_mode must be auto, bars or ticks")
    if research_data_mode == "bars" and any(
        ticks_mode(getattr(instance, name)) == "required"
        for name in ("compute_indicators", "entry_strategy", "exit_strategy")
    ):
        raise TickDataRequiredError(f"{strategy_class.__name__} requires tick data; use auto or ticks data mode.")
    checkpoint()

    use_ticks = research_data_mode == "ticks" or (
        research_data_mode == "auto"
        and any(
            ticks_mode(getattr(instance, name)) != "none"
            for name in ("compute_indicators", "entry_strategy", "exit_strategy")
        )
    )
    while True:
        store = None
        if use_ticks:
            tf = normalize_timeframe(timeframe)
            if tf in ("W1", "MN1"):
                raise ValueError(f"Unsupported timeframe '{timeframe}' for tick store bars")
            on_phase("loading_data", f"Synchronizing ticks for {symbol}")
            store = TickStore(symbol)
            report = store.sync(start=start, end=end, progress=False)
            checkpoint()
            if report.failed:
                failures = "; ".join(
                    f"{day.isoformat()}: {report.errors.get(day, 'download failed')}" for day in report.failed
                )
                raise ValueError(f"Tick synchronization failed for {failures}. Check the MT5 gateway and retry.")
            on_phase("loading_data", f"Building {timeframe} candles from ticks for {symbol}")
            frame = store.bars(timeframe, start=start, end=end)
            frame = drop_forming_bar(frame, timeframe, now=datetime.now(BRASILIA_TZ))
        else:
            on_phase("loading_data", f"Loading {timeframe} candles for {symbol}")
            frame = _bars_frame(market_data_service, symbol=symbol, timeframe=timeframe, start=start, end=end)
        if frame.empty:
            raise ValueError("No market data found for the given parameters.")
        checkpoint()

        on_phase("simulating", f"Simulating {len(frame)} candles")
        try:
            result = backtest(
                frame,
                strategy=instance,
                symbol=symbol,
                quantity=quantity,
                point_value=point_value,
                initial_capital=initial_capital,
                costs=costs,
                exit_params=dict(exit_params) if exit_params else None,
                day_trade=day_trade,
                day_trade_start_time=day_trade_start_time,
                day_trade_end_time=day_trade_end_time,
                day_trade_close_time=day_trade_close_time,
                post_exit_bars=post_exit_bars,
                ticks=store,
                check_causality=True,
                progress=False,
            )
        except TickDataRequiredError:
            if research_data_mode != "auto" or use_ticks:
                raise
            checkpoint()
            on_phase("loading_data", "Strategy requires ticks; restarting with tick history")
            # Discard every decision and all instance state from the bar attempt.
            instance = strategy_class(**params)
            use_ticks = True
            continue
        break
    checkpoint()
    request = build_import_request(result, name=frozen.strategy_id, timeframe=timeframe)
    order_kinds = set(result.order_kinds)
    info = validate_bundle(frozen.bundle, bundle_root)
    if info.uses_tick_hooks:
        order_kinds.add("tick_hooks")
    return StrategyRunResult(
        request=request,
        ticks_used=store is not None,
        order_kinds=sorted(order_kinds),
    )


def _require_decision_timeframe(strategy_class: type, timeframe: str) -> None:
    """A strategy that declares a decision timeframe runs only on frames of that timeframe."""
    declared = getattr(strategy_class, "decision_timeframe", None)
    if declared is not None and normalize_timeframe(str(declared)) != normalize_timeframe(timeframe):
        raise ValueError(
            f"{strategy_class.__name__} decides on {declared} bars and cannot run on {timeframe} bars; "
            f"run it on {declared}"
        )


def _bars_frame(
    market_data_service: Any, *, symbol: str, timeframe: str, start: datetime, end: datetime
) -> pd.DataFrame:
    """Completed bars in ``[start, end]`` shaped as ``load_bars()`` returns them."""
    start_ts = _brasilia(start)
    end_ts = _brasilia(end)
    now = datetime.now(BRASILIA_TZ)
    bars = market_data_service.get_ohlcv(symbol, timeframe, start, end)
    frame = ohlcv_models_to_frame(bars)
    frame = filter_index_range(frame, start_ts, end_ts)
    frame = drop_forming_bar(frame, timeframe, now=now)
    if not frame.empty:
        validate_bars_frame(frame)
    return attach_metadata(
        frame,
        symbol=symbol,
        timeframe=timeframe,
        source=MT5_SOURCE,
        requested_start=start_ts,
        requested_end=end_ts,
    )


def _brasilia(value: datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(BRASILIA_TZ) if stamp.tzinfo is None else stamp.tz_convert(BRASILIA_TZ)
