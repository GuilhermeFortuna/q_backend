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
from typing import Any, Dict, Optional

import pandas as pd

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.entry_config import (
    format_entry_strategy_label,
    normalize_entries,
)
from q_backend.backtesting.factory import build_composite_entry
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.market_data.clients.metatrader import _to_naive_local
from q_backend.optimization.metrics import build_equity_curve
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, RunStatus
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

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "backtest"

BacktestJobRequest = BacktestRequest


def _strategy_persist_name(request: BacktestJobRequest) -> str:
    entries, manager, _exit_params = normalize_entries(request)
    if request.entries is not None:
        return format_entry_strategy_label(entries, manager)
    return request.strategy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_config(request: BacktestJobRequest) -> Dict[str, Any]:
    config = request.model_dump(mode="json")
    if request.engine == "tick":
        config["timeframe"] = "TICK"
    return config


def _persist_progress(run_id: str, status: str, error: Optional[str] = None) -> None:
    try:
        set_job_progress(
            get_redis(),
            run_id,
            {"run_id": run_id, "status": status, "error": error},
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
        logger.debug("Redis progress unavailable for backtest run %s", run_id)


def _persist_run_start(request: BacktestJobRequest) -> str:
    """Create (or reuse) the backtest run row and return its id."""
    config = _request_config(request)
    persisted_timeframe = config.get("timeframe", request.timeframe)
    now = _now()
    with session_scope() as session:
        get_or_create_strategy(session, name=_strategy_persist_name(request))
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
        )
        return str(run.id)


def start_job(request: BacktestJobRequest) -> str:
    """Persist the run and dispatch it to the worker pool. Returns the run id."""
    run_id = _persist_run_start(request)
    _persist_progress(run_id, "running")

    from q_backend.tasks import actors

    actors.run_backtest.send(run_id, request.model_dump_json())
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
    request: BacktestJobRequest, start: datetime, end: datetime, market_data_service
) -> tuple[dict[str, Any], list, pd.DataFrame]:
    ohlcv_data = market_data_service.get_ohlcv(
        request.symbol, request.timeframe, start, end
    )
    if not ohlcv_data:
        raise ValueError("No market data found for the given parameters.")

    df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
    df.set_index("time", inplace=True)
    df.index = pd.to_datetime(df.index, format="ISO8601")

    entries, manager, exit_params = normalize_entries(request)
    strategy = build_composite_entry(
        [
            {"strategy": entry.strategy, "params": entry.params}
            for entry in entries
        ],
        manager.kind,
        manager.params,
        exit_params,
        request.symbol,
    )
    chart_data = serialize_chart_data(strategy.compute_indicators(df.copy()), strategy)

    sizer = build_position_sizer(
        request.position_sizing, point_value=request.point_value
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
    )
    registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

    metrics = registry.get_performance_metrics(request.initial_capital)
    closed_trade_objects = registry.get_closed_trades()
    trades = [t.model_dump(mode="json") for t in closed_trade_objects]
    equity = build_equity_curve(
        closed_trade_objects, request.initial_capital, start, end
    )
    equity_df = pd.DataFrame({"time": equity.index, "equity": equity.values})

    payload = {
        "metrics": metrics,
        "trades": trades,
        "bars": chart_data["bars"],
        "indicators": chart_data["indicators"],
    }
    return payload, closed_trade_objects, equity_df


def _execute_tick(
    request: BacktestJobRequest, start: datetime, end: datetime, market_data_service
) -> tuple[dict[str, Any], list, pd.DataFrame]:
    from q_backend.optimization.tick_backtest_runner import resolve_tick_flags

    arrays = market_data_service.get_ticks_columnar(
        request.symbol, start, end, flags=resolve_tick_flags(request.tick_flags)
    )
    if len(arrays["time_msc"]) == 0:
        raise ValueError("No tick data found for the given parameters.")

    ticks = TickArrays(
        time_msc=arrays["time_msc"],
        bid=arrays["bid"],
        ask=arrays["ask"],
        last=arrays["last"],
        volume=arrays["volume"],
    )
    strategy = build_tick_strategy(
        request.strategy, request.strategy_params, request.symbol
    )
    chart_data = serialize_tick_chart_data(
        ticks, strategy, display_timeframe=request.display_timeframe
    )
    sizing_config = request.position_sizing or FixedQuantityPositionSizing()
    engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing_config,
        initial_capital=request.initial_capital,
        point_value=request.point_value,
        symbol=request.symbol,
    )
    registry = engine.run(ticks, parallel_mode=ParallelMode.DAY_TRADE)

    metrics = registry.get_performance_metrics(request.initial_capital)
    closed_trade_objects = registry.get_closed_trades()
    trades = [t.model_dump(mode="json") for t in closed_trade_objects]
    equity = build_equity_curve(
        closed_trade_objects, request.initial_capital, start, end
    )
    equity_df = pd.DataFrame({"time": equity.index, "equity": equity.values})

    payload = {
        "metrics": metrics,
        "trades": trades,
        "bars": chart_data["bars"],
        "indicators": chart_data["indicators"],
    }
    return payload, closed_trade_objects, equity_df


def run_backtest_job(run_id: str, request_json: str) -> None:
    """Worker entry point: execute the backtest and persist its results."""
    request = BacktestJobRequest.model_validate_json(request_json)
    from q_backend.tasks.worker_context import get_worker_market_data_service

    market_data_service = get_worker_market_data_service()
    try:
        start, end = _resolve_range(request)
        if request.engine == "tick":
            payload, trades_objects, equity_df = _execute_tick(
                request, start, end, market_data_service
            )
        else:
            payload, trades_objects, equity_df = _execute_candle(
                request, start, end, market_data_service
            )
        payload["run_id"] = run_id

        lake_paths = None
        try:
            trades_df = pd.DataFrame([t.model_dump() for t in trades_objects])
            lake_paths = write_backtest_artifacts(run_id, trades_df, equity_df)
            lake_paths["result"] = write_backtest_result(run_id, payload)
        except Exception:  # noqa: BLE001 - best-effort persistence/progress; logged and degraded
            logger.warning(
                "Failed to write backtest lake artifacts for %s", run_id, exc_info=True
            )

        _finish_run(
            run_id,
            status=RunStatus.COMPLETED.value,
            result_summary=payload["metrics"],
            lake_paths=lake_paths,
        )
        _persist_progress(run_id, "completed")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Backtest job %s failed", run_id)
        _finish_run(run_id, status=RunStatus.FAILED.value, error_message=str(exc))
        _persist_progress(run_id, "failed", error=str(exc))


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
        logger.warning(
            "Failed to persist backtest run finish for %s", run_id, exc_info=True
        )


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
            "Redis progress unavailable for run %s; using DB status", run_id,
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
