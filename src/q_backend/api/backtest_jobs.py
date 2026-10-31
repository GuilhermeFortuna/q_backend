"""Job manager for asynchronous backtest runs.

Backtests used to execute synchronously inside the request handler. They now run as
Dramatiq jobs like optimization/walk-forward/discovery: the API persists a run and
enqueues a single worker message; the worker executes the backtest, writes the full
chart payload to the lake, and updates status. Status/result are read from Redis/DB
and the lake — no execution happens in the API process.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import pandas as pd

from q_backend.api.backtest_progress import BacktestProgressReporter
from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.analytics import compute_backtest_performance
from q_backend.backtesting.excursions import DealBar, measure_trades
from q_backend.backtesting.chart_data import market_data_frame, serialize_chart_data
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.entry_config import (
    format_entry_strategy_label,
    normalize_entries,
)
from q_backend.backtesting.factory import build_composite_entry, wrap_with_ml_filter
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.tick.chart_data import serialize_tick_backtest_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.backtesting.run_import import complete_stack_run
from q_backend.market_data.clients.metatrader import _to_naive_local
from q_backend.optimization.metrics import build_equity_curve
from q_backend.research.strategy_file_runs import FrozenStrategy, run_strategy
from q_backend.research.strategy_files import default_bundle_root, strategy_dir
from q_backend.research.strategy_versions import (
    ResolvedStrategyRun,
    StrategyVersionNotFound,
    bundle_of,
    check_strategy_run_request,
)
from q_backend.research.strategy_versions import resolve_strategy_run as resolve_version_run
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, ResearchStrategyVersion, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    find_backtest_run_by_config,
    get_or_create_strategy,
    mark_active_runs_cancelled,
    update_backtest_run,
)
from q_backend.storage.lake.artifacts import (
    write_backtest_artifacts,
    write_backtest_result,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    delete_job_progress,
    get_job_progress,
    set_job_progress,
)
from q_backend.tasks.fanin import clear_job_keys, is_cancelled, set_cancelled

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "backtest"

BacktestJobRequest = BacktestRequest

CANCELLED_MESSAGE = "Cancelled by operator."

Checkpoint = Callable[[], None]


class BacktestCancelled(Exception):
    """Raised at a checkpoint when an operator cancelled the run."""


def _no_checkpoint() -> None:
    return None


def _cancel_checkpoint(run_id: str) -> Checkpoint:
    """A checkpoint that aborts the worker once ``run_id`` has been cancelled."""

    def checkpoint() -> None:
        if is_cancelled(run_id):
            raise BacktestCancelled(run_id)

    return checkpoint


def _strategy_persist_name(request: BacktestJobRequest, strategy_name: Optional[str] = None) -> str:
    if strategy_name is not None:
        return strategy_name
    entries, manager, _exit_params = normalize_entries(request)
    if request.entries is not None:
        return format_entry_strategy_label(entries, manager)
    return request.strategy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_config(request: BacktestJobRequest, strategy_name: Optional[str] = None) -> Dict[str, Any]:
    config = request.model_dump(mode="json")
    if strategy_name is not None:
        config["research_data_mode"] = request.research_data_mode
        # A strategy-file run is keyed by its frozen version, so an edited file never reuses an old run.
        # Its config names the version, not the strategy: re-running it posts this config back unchanged,
        # and a request may name a strategy version or a strategy, not both.
        config.pop("strategy", None)
        return config
    if request.engine == "tick":
        config["timeframe"] = "TICK"
    else:
        from q_backend.backtesting.candle_kernel import _exit_params

        _, _, exit_params = normalize_entries(request)
        if float(exit_params.get("breakeven_trigger_pct", 0.0)) > 0.0:
            config["exit_params"] = _exit_params(exit_params)
    return config


def _publish_progress(run_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(get_redis(), run_id, payload, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.debug("Redis progress unavailable for backtest run %s", run_id)


def _persist_progress(run_id: str, status: str, error: Optional[str] = None) -> None:
    _publish_progress(run_id, {"run_id": run_id, "status": status, "error": error})


def _job_reporter(run_id: str, *, started_at: Optional[datetime] = None) -> BacktestProgressReporter:
    """Progress reporter for a run. The worker resumes the start time the API stored."""
    if started_at is None:
        started_at = _stored_started_at(run_id) or _now()
    return BacktestProgressReporter(
        run_id,
        started_at=started_at,
        publish=lambda payload: _publish_progress(run_id, payload),
    )


def _stored_started_at(run_id: str) -> Optional[datetime]:
    try:
        stored = get_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort progress; the worker falls back to its own clock
        logger.debug("Redis progress unavailable for backtest run %s", run_id)
        return None
    raw = (stored or {}).get("started_at")
    return datetime.fromisoformat(raw) if raw else None


def _persist_run_start(request: BacktestJobRequest, strategy_name: Optional[str] = None) -> str:
    """Create (or reuse) the backtest run row and return its id."""
    config = _request_config(request, strategy_name)
    persisted_timeframe = config.get("timeframe", request.timeframe)
    now = _now()
    with session_scope() as session:
        get_or_create_strategy(session, name=_strategy_persist_name(request, strategy_name))
        existing = find_backtest_run_by_config(session, config)
        if existing is not None:
            update_backtest_run(
                session,
                existing.id,
                status=RunStatus.RUNNING.value,
                started_at=now,
                clear_error_message=True,
            )
            return str(existing.id)
        bt_config = create_backtest_config(
            session,
            name=f"{request.symbol}-{persisted_timeframe}",
            config=config,
        )
        run = create_backtest_run(
            session,
            backtest_config_id=bt_config.id,
            config=config,
            status=RunStatus.RUNNING.value,
            started_at=now,
            strategy_version_id=uuid.UUID(config["strategy_version_id"]) if strategy_name is not None else None,
        )
        return str(run.id)


def validate_ml_filter_request(request: BacktestJobRequest) -> None:
    """Fail an incompatible or unsupported ML-filter request before a run is persisted.

    Raises ``MLFilterRequestError`` (422), ``MLFilterCompatibilityError`` (409) or
    ``MLFilterModelUnavailableError`` (404). Requests without a filter are untouched.
    """
    from q_backend.ml_filters import service as ml_filter_service
    from q_backend.ml_filters.compatibility import uses_ml_filter

    config = request.model_dump(mode="json")
    if not uses_ml_filter(config):
        return
    ml_filter_service.validate_filter_request_shape(config)
    manifest = ml_filter_service.read_filter_manifest(config["ml_filter"]["model_version_id"])
    ml_filter_service.validate_filter_compatibility(config, manifest)


def resolve_strategy_run(request: BacktestJobRequest) -> ResolvedStrategyRun:
    """Freeze or load the strategy version a request names, and coerce its parameters.

    Raises ``StrategyFileError`` or ``ParamError`` (422), ``StrategyFileRequestError`` (422) and
    ``StrategyVersionNotFound`` (404). The stack does not run ``entries`` or ``strategy`` with a version.
    """
    check_strategy_run_request(
        engine=request.engine,
        ml_filter=request.ml_filter,
        position_sizing=request.position_sizing,
    )
    with session_scope() as session:
        return resolve_version_run(
            session,
            strategy_file=request.strategy_file,
            strategy_version_id=request.strategy_version_id,
            strategy_params=request.strategy_params,
            root=strategy_dir(),
            bundle_root=default_bundle_root(),
        )


def start_job(request: BacktestJobRequest) -> str:
    """Persist the run and dispatch it to the worker pool. Returns the run id."""
    validate_ml_filter_request(request)
    strategy: Optional[ResolvedStrategyRun] = None
    if request.strategy_file is not None or request.strategy_version_id is not None:
        strategy = resolve_strategy_run(request)
        request = request.model_copy(
            update={
                "strategy_file": None,
                "strategy_version_id": strategy.version_id,
                "strategy_params": strategy.strategy_params,
            }
        )
    run_id = _persist_run_start(request, strategy.strategy_id if strategy is not None else None)
    # A reused run row may carry the flag of an earlier cancellation.
    clear_job_keys(run_id)
    _job_reporter(run_id, started_at=_now()).phase("queued", "Waiting for a worker")

    from q_backend.tasks import actors

    # The worker takes the strategy from the version, so the dispatched request omits ``strategy``.
    actors.run_backtest.send(
        run_id,
        request.model_dump_json(exclude={"strategy"} if strategy is not None else None),
    )
    return run_id


def _resolve_range(request: BacktestJobRequest) -> tuple[datetime, datetime]:
    start = request.start or (datetime.now() - timedelta(days=365))
    end = request.end or datetime.now()
    start = _to_naive_local(start)
    end = _to_naive_local(end)
    if start >= end:
        raise ValueError("Start datetime must be before end datetime.")
    return start, end


def _execute_candle(
    request: BacktestJobRequest,
    start: datetime,
    end: datetime,
    market_data_service,
    reporter: Optional[BacktestProgressReporter] = None,
    checkpoint: Checkpoint = _no_checkpoint,
) -> tuple[dict[str, Any], list, pd.DataFrame, pd.DataFrame]:
    reporter = reporter or BacktestProgressReporter.silent()
    checkpoint()
    reporter.phase("loading_data", f"Loading {request.timeframe} candles for {request.symbol}")
    with reporter.read("loading_candles", start, end):
        ohlcv_data = market_data_service.get_ohlcv(request.symbol, request.timeframe, start, end)
    checkpoint()
    if not ohlcv_data:
        raise ValueError("No market data found for the given parameters.")

    df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
    df.set_index("time", inplace=True)
    df.index = pd.to_datetime(df.index, format="ISO8601")
    reporter.phase("preparing", "Preparing strategy and indicators")

    entries, manager, exit_params = normalize_entries(request)
    strategy = build_composite_entry(
        [{"strategy": entry.strategy, "params": entry.params} for entry in entries],
        manager.kind,
        manager.params,
        exit_params,
        request.symbol,
    )
    ml_filter_manifest = None
    if request.ml_filter is not None:
        from q_backend.ml_filters import service as ml_filter_service
        from q_backend.ml_filters.compatibility import to_utc

        # One pinned pipeline per job; entries before train_end are warm-up only.
        fitted_model, ml_filter_manifest = ml_filter_service.resolve_filter_model(request.model_dump(mode="json"))
        strategy = wrap_with_ml_filter(
            strategy,
            fitted_model,
            request.ml_filter.threshold,
            entry_start=to_utc(datetime.fromisoformat(ml_filter_manifest["train_end"])).to_pydatetime(),
        )
    augmented = augment_indicator_frame(strategy, df)
    chart_data = serialize_chart_data(augmented, strategy)
    market_data = market_data_frame(augmented)

    sizer = build_position_sizer(request.position_sizing, point_value=request.point_value)
    from q_backend.backtesting.intrabar import (
        ReplayReport,
        breakeven_market_inputs,
        classify_exit_resolutions,
        intrabar_notes,
    )

    replay_report = ReplayReport()

    def on_replay(bar: int, bars_total: int, candle_time: pd.Timestamp) -> None:
        checkpoint()
        reporter.replay(
            bar,
            bars_total,
            candle_time,
            replayed=len(replay_report.replayed),
            substituted=len(replay_report.substituted),
        )

    engine = BacktestEngine(
        strategy,
        sizer,
        initial_capital=request.initial_capital,
        point_values={request.symbol: request.point_value},
        day_trade=request.day_trade,
        day_trade_start_time=request.day_trade_start_time,
        day_trade_end_time=request.day_trade_end_time,
        day_trade_close_time=request.day_trade_close_time,
        costs=request.costs,
        **breakeven_market_inputs(
            strategy,
            market_data_service,
            request.timeframe,
            report=replay_report,
            on_replay_completed=on_replay,
            read_context=reporter.read,
        ),
    )
    checkpoint()
    reporter.phase("simulating", f"Simulating {len(df)} candles", bars_total=len(df))
    registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)
    checkpoint()

    metrics = registry.get_performance_metrics(request.initial_capital)
    closed_trade_objects = registry.get_closed_trades()
    classify_exit_resolutions(closed_trade_objects, replay_report)
    reporter.phase("measuring", "Measuring trade excursions")
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    measurement_report = ReplayReport()
    measurement_replay = MarketTickReplay(
        market_data_service, request.symbol, request.timeframe, df, report=measurement_report
    )

    def measurement_deals(bar: int) -> DealBar:
        time_us, prices = measurement_replay(bar)
        resolutions = [measurement_report.resolutions.get(int(value)) for value in time_us]
        resolution = next((r for r in ("candle", "minute") if r in resolutions), "tick")
        return DealBar(pd.to_datetime(time_us, unit="us", utc=True), prices, resolution)

    measure_trades(
        closed_trade_objects,
        df,
        deals=measurement_deals,
        post_exit_bars=request.post_exit_bars,
        day_trade_close=request.day_trade_close_time if request.day_trade else None,
        on_trade=reporter.measured,
    )
    notes = intrabar_notes(closed_trade_objects, replay_report)
    trades = [t.model_dump(mode="json") for t in closed_trade_objects]
    equity = build_equity_curve(closed_trade_objects, request.initial_capital, start, end)
    equity_df = pd.DataFrame({"time": equity.index, "equity": equity.values})

    payload = {
        "metrics": metrics,
        "trades": trades,
        "bars": chart_data["bars"],
        "indicators": chart_data["indicators"],
        "notes": notes,
        "performance": compute_backtest_performance(
            closed_trade_objects,
            request.initial_capital,
            start,
            end,
            post_exit_bars=request.post_exit_bars,
        ).model_dump(mode="json"),
    }
    if request.ml_filter is not None and ml_filter_manifest is not None:
        payload["ml_filter"] = request.ml_filter.model_dump(mode="json")
        payload["ml_filter_summary"] = _ml_filter_summary(
            strategy,
            ml_filter_manifest,
            request,
            executed_trades=len(closed_trade_objects) + len(registry.get_open_trades()),
        )
    return payload, closed_trade_objects, equity_df, market_data


def _ml_filter_summary(
    strategy: Any, manifest: dict[str, Any], request: BacktestJobRequest, *, executed_trades: int
) -> dict[str, Any]:
    """Candidate-level filter counts, kept apart from executed-trade metrics."""
    assert request.ml_filter is not None
    return {
        **strategy.diagnostics_summary(),
        "executed_trades": executed_trades,
        "model_version_id": request.ml_filter.model_version_id,
        "dataset_id": manifest["dataset_id"],
        "algorithm": manifest["algorithm"],
        "train_end": manifest["train_end"],
        "compatibility_fingerprint": manifest["compatibility_fingerprint"],
    }


def _execute_tick(
    request: BacktestJobRequest,
    start: datetime,
    end: datetime,
    market_data_service,
    reporter: Optional[BacktestProgressReporter] = None,
    checkpoint: Checkpoint = _no_checkpoint,
) -> tuple[dict[str, Any], list, pd.DataFrame, pd.DataFrame]:
    from q_backend.optimization.tick_backtest_runner import resolve_tick_flags

    reporter = reporter or BacktestProgressReporter.silent()
    checkpoint()
    reporter.phase("loading_data", f"Loading ticks for {request.symbol}")
    with reporter.read("loading_ticks", start, end):
        arrays = market_data_service.get_ticks_columnar(
            request.symbol, start, end, flags=resolve_tick_flags(request.tick_flags)
        )
    if len(arrays["time_msc"]) == 0:
        raise ValueError("No tick data found for the given parameters.")
    checkpoint()
    reporter.phase("preparing", "Preparing strategy and indicators")

    ticks = TickArrays(
        time_msc=arrays["time_msc"],
        bid=arrays["bid"],
        ask=arrays["ask"],
        last=arrays["last"],
        volume=arrays["volume"],
    )
    strategy = build_tick_strategy(request.strategy, request.strategy_params, request.symbol)
    chart_data, market_data = serialize_tick_backtest_data(ticks, strategy, display_timeframe=request.display_timeframe)
    sizing_config = request.position_sizing or FixedQuantityPositionSizing()
    engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing_config,
        initial_capital=request.initial_capital,
        point_value=request.point_value,
        symbol=request.symbol,
    )
    checkpoint()
    reporter.phase("simulating", f"Simulating {len(ticks.time_msc)} ticks")
    registry = engine.run(ticks, parallel_mode=ParallelMode.DAY_TRADE, checkpoint=checkpoint)
    checkpoint()

    metrics = registry.get_performance_metrics(request.initial_capital)
    closed_trade_objects = registry.get_closed_trades()
    reporter.phase("measuring", "Measuring trade excursions")
    bars = market_data.copy()
    bars.index = pd.DatetimeIndex(pd.to_datetime(bars.pop("time"), utc=True), name="time")

    def tick_deals(bar: int) -> DealBar:
        start_us = int(pd.Timestamp(bars.index[bar]).timestamp() * 1_000_000)
        if bar + 1 < len(bars):
            end_us = int(pd.Timestamp(bars.index[bar + 1]).timestamp() * 1_000_000)
        else:
            from q_backend.market_data.exogenous_context import bar_duration

            end_us = start_us + int(bar_duration(request.display_timeframe).total_seconds() * 1_000_000)
        tick_us = ticks.time_msc.astype("int64") * 1000
        selected = (tick_us >= start_us) & (tick_us < end_us)
        return DealBar(pd.to_datetime(tick_us[selected], unit="us", utc=True), ticks.last[selected], "tick")

    measure_trades(
        closed_trade_objects,
        bars,
        deals=tick_deals,
        post_exit_bars=request.post_exit_bars,
        day_trade_close=request.day_trade_close_time if request.day_trade else None,
        on_trade=reporter.measured,
    )
    trades = [t.model_dump(mode="json") for t in closed_trade_objects]
    equity = build_equity_curve(closed_trade_objects, request.initial_capital, start, end)
    equity_df = pd.DataFrame({"time": equity.index, "equity": equity.values})

    payload = {
        "metrics": metrics,
        "trades": trades,
        "bars": chart_data["bars"],
        "indicators": chart_data["indicators"],
        "notes": [],
        "performance": compute_backtest_performance(
            closed_trade_objects,
            request.initial_capital,
            start,
            end,
            post_exit_bars=request.post_exit_bars,
        ).model_dump(mode="json"),
    }
    return payload, closed_trade_objects, equity_df, market_data


def run_backtest_job(run_id: str, request_json: str) -> None:
    """Worker entry point: execute the backtest and persist its results."""
    request = BacktestJobRequest.model_validate_json(request_json)
    from q_backend.tasks.worker_context import get_worker_market_data_service

    market_data_service = get_worker_market_data_service()
    reporter = _job_reporter(run_id)
    checkpoint = _cancel_checkpoint(run_id)
    try:
        checkpoint()
        start, end = _resolve_range(request)
        if request.strategy_version_id is not None:
            _run_strategy_version(run_id, request, start, end, market_data_service, reporter, checkpoint)
            return
        if request.engine == "tick":
            payload, trades_objects, equity_df, market_data = _execute_tick(
                request, start, end, market_data_service, reporter, checkpoint
            )
        else:
            payload, trades_objects, equity_df, market_data = _execute_candle(
                request, start, end, market_data_service, reporter, checkpoint
            )
        payload["run_id"] = run_id
        checkpoint()
        reporter.phase("saving", "Saving results")

        lake_paths = None
        try:
            trades_df = pd.DataFrame([t.model_dump() for t in trades_objects])
            lake_paths = write_backtest_artifacts(run_id, trades_df, equity_df, market_data)
            lake_paths["result"] = write_backtest_result(run_id, payload)
        except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
            logger.warning("Failed to write backtest lake artifacts for %s", run_id, exc_info=True)

        result_summary = payload["metrics"]
        if "ml_filter_summary" in payload:
            result_summary = {**result_summary, "ml_filter_summary": payload["ml_filter_summary"]}
        _finish_run(
            run_id,
            status=RunStatus.COMPLETED.value,
            result_summary=result_summary,
            lake_paths=lake_paths,
        )
        _persist_progress(run_id, "completed")
    except BacktestCancelled:
        # request_cancel already recorded the terminal state; nothing was written.
        logger.info("Backtest job %s cancelled", run_id)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Backtest job %s failed", run_id)
        _finish_run(run_id, status=RunStatus.FAILED.value, error_message=str(exc))
        _persist_progress(run_id, "failed", error=str(exc))


def _run_strategy_version(
    run_id: str,
    request: BacktestJobRequest,
    start: datetime,
    end: datetime,
    market_data_service,
    reporter: BacktestProgressReporter,
    checkpoint: Checkpoint,
) -> None:
    """Run a frozen strategy version and store its result on the job's run row."""
    assert request.strategy_version_id is not None
    with session_scope() as session:
        version = session.get(ResearchStrategyVersion, uuid.UUID(request.strategy_version_id))
        if version is None:
            raise StrategyVersionNotFound(f"Strategy version '{request.strategy_version_id}' not found.")
        frozen = FrozenStrategy(
            strategy_id=version.strategy_id,
            bundle=bundle_of(version),
            param_specs=list(version.param_specs),
        )
    quantity = check_strategy_run_request(
        engine=request.engine,
        ml_filter=request.ml_filter,
        position_sizing=request.position_sizing,
    )
    outcome = run_strategy(
        frozen,
        research_data_mode=request.research_data_mode,
        bundle_root=default_bundle_root(),
        symbol=request.symbol,
        timeframe=request.timeframe,
        start=start,
        end=end,
        strategy_params=request.strategy_params,
        quantity=quantity,
        point_value=request.point_value,
        initial_capital=request.initial_capital,
        costs=request.costs,
        exit_params=request.exit_params,
        day_trade=request.day_trade,
        day_trade_start_time=request.day_trade_start_time,
        day_trade_end_time=request.day_trade_end_time,
        day_trade_close_time=request.day_trade_close_time,
        post_exit_bars=request.post_exit_bars,
        market_data_service=market_data_service,
        checkpoint=checkpoint,
        on_phase=lambda phase, message: reporter.phase(phase, message),
    )
    checkpoint()
    reporter.phase("saving", "Saving results")
    summary = {
        **outcome.request.result.metrics,
        "ticks_used": outcome.ticks_used,
        "order_kinds": outcome.order_kinds,
    }
    with session_scope() as session:
        run = session.get(BacktestRun, uuid.UUID(run_id))
        if run is not None and run.status == RunStatus.CANCELLED.value:
            # Cancelled while the worker was finishing: the cancellation stands.
            return
        complete_stack_run(session, uuid.UUID(run_id), outcome.request, result_summary=summary)
    _persist_progress(run_id, "completed")


def _finish_run(
    run_id: str,
    *,
    status: str,
    result_summary: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    lake_paths: Optional[dict[str, str]] = None,
) -> None:
    try:
        with session_scope() as session:
            run = session.get(BacktestRun, uuid.UUID(run_id))
            if run is not None and run.status == RunStatus.CANCELLED.value:
                # Cancelled while the worker was finishing: the cancellation stands.
                return
            update_backtest_run(
                session,
                uuid.UUID(run_id),
                status=status,
                result_summary=result_summary,
                error_message=error_message,
                lake_paths=lake_paths,
                finished_at=_now(),
            )
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.warning("Failed to persist backtest run finish for %s", run_id, exc_info=True)


def request_cancel(run_id: str) -> Optional[dict[str, Any]]:
    """Cancel an active run and return its status; ``None`` if the run is unknown.

    Cancellation is cooperative: the cancel flag stops the worker at its next
    checkpoint, while the run is marked cancelled (and the terminal event recorded)
    right away so clients leave the running state without waiting for the worker.
    A run that already finished keeps its final status.
    """
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            run = session.get(BacktestRun, run_uuid)
            if run is None:
                return None
            active = run.status in (RunStatus.PENDING.value, RunStatus.RUNNING.value)
            if active:
                update_backtest_run(
                    session,
                    run_uuid,
                    status=RunStatus.CANCELLED.value,
                    error_message=CANCELLED_MESSAGE,
                    finished_at=_now(),
                )
    except Exception:  # noqa: BLE001 - surfaced below as the unchanged status
        logger.warning("Failed to cancel backtest run %s", run_id, exc_info=True)
        return get_status_payload(run_id)
    if active:
        set_cancelled(run_id)
        _persist_progress(run_id, "cancelled", error=CANCELLED_MESSAGE)
    return get_status_payload(run_id)


def get_status_payload(run_id: str) -> Optional[dict[str, Any]]:
    """Status from live Redis progress while running, else the DB run row."""
    db_status = _db_status(run_id)
    try:
        cached = get_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - Redis progress is a best-effort cache
        # Best-effort: the live Redis progress is an optimization over the
        # authoritative DB run row. If Redis is unreachable, fall back to the DB
        # status rather than failing the status endpoint; log for visibility.
        logger.warning(
            "Redis progress unavailable for run %s; using DB status",
            run_id,
            exc_info=True,
        )
        cached = None

    if db_status is None:
        return cached
    if db_status.get("status") == "running" and cached is not None:
        return cached
    return db_status


def _db_status(run_id: str) -> Optional[dict[str, Any]]:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            run = session.get(BacktestRun, run_uuid)
            if run is None:
                return None
            status_map = {
                RunStatus.RUNNING.value: "running",
                RunStatus.PENDING.value: "running",
                RunStatus.COMPLETED.value: "completed",
                RunStatus.FAILED.value: "failed",
                RunStatus.CANCELLED.value: "cancelled",
            }
            return {
                "run_id": run_id,
                "status": status_map.get(run.status, run.status),
                "error": run.error_message,
            }
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.warning("Failed to read backtest run %s status", run_id, exc_info=True)
        return None


def reconcile_orphaned_runs() -> int:
    """Cancel backtest runs left active by a previous process. Call once on startup."""
    try:
        with session_scope() as session:
            count = mark_active_runs_cancelled(
                session,
                BacktestRun,
                error_message="Cancelled after backend restart (run was orphaned).",
            )
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.warning("Failed to reconcile orphaned backtest runs: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned backtest run(s) on startup.", count)
    return count


def evict_run(run_id: str) -> None:
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.debug("Redis progress delete unavailable for backtest run %s", run_id)
