"""Job manager for asynchronous walk-forward analysis runs.

Walk-forward runs are long and serialized via a single-worker thread pool.
Progress snapshots are mirrored to Redis under the ``walkforward`` namespace;
execution state and full results remain in-memory until the run finishes.
"""

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

import pandas as pd
from pydantic import BaseModel

from q_backend.optimization.backtest_runner import BacktestRunner, DefaultBacktestRunner
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardProgress,
    WalkForwardResult,
    WalkForwardRunner,
    WalkForwardWindowResult,
    resolve_worker_count,
    split_windows,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import RunStatus, WalkForwardRun
from q_backend.storage.db.repositories import (
    create_walkforward_run,
    create_walkforward_window,
    get_walkforward_run,
    mark_active_runs_cancelled,
    update_walkforward_run,
)
from q_backend.storage.lake.artifacts import (
    delete_walkforward_artifacts,
    read_walkforward_artifact,
    write_walkforward_artifacts,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    delete_job_progress,
    get_job_progress,
    set_job_progress,
)

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "walkforward"

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

_FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled"})


class WalkForwardRequest(BaseModel):
    optimization: OptimizationConfig
    walkforward: WalkForwardConfig


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_run_uuid(run_id: str) -> uuid.UUID:
    if len(run_id) == 32:
        return uuid.UUID(hex=run_id)
    return uuid.UUID(run_id)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return _as_utc(dt).isoformat()


@dataclass
class WalkForwardJob:
    run_id: str
    request: WalkForwardRequest
    db_run_id: Optional[uuid.UUID] = None
    status: JobStatus = "pending"
    current_window: int = 0
    total_windows: int = 0
    workers: int = 1
    phase: Optional[Literal["optimizing", "testing"]] = None
    windows_completed: int = 0
    result: Optional[WalkForwardResult] = None
    lake_paths: Optional[dict[str, str]] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


_jobs: dict[str, WalkForwardJob] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkforward")


def get_job(run_id: str) -> Optional[WalkForwardJob]:
    with _lock:
        return _jobs.get(run_id)


def evict_run(run_id: str) -> None:
    with _lock:
        _jobs.pop(run_id, None)
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug("Redis progress delete unavailable for walk-forward run %s", run_id)


def _request_config_payload(request: WalkForwardRequest) -> dict[str, Any]:
    return {
        "optimization": request.optimization.model_dump(mode="json"),
        "walkforward": request.walkforward.model_dump(mode="json"),
    }


def _persist_progress(job: WalkForwardJob) -> None:
    try:
        set_job_progress(
            get_redis(),
            job.run_id,
            status_payload(job),
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:
        logger.debug("Redis progress unavailable for walk-forward run %s", job.run_id)


def _persist_run_start(request: WalkForwardRequest) -> tuple[str, Optional[uuid.UUID]]:
    try:
        with session_scope() as session:
            run = create_walkforward_run(
                session,
                name=request.optimization.study.name,
                config=_request_config_payload(request),
                status=RunStatus.PENDING.value,
            )
            return run.id.hex, run.id
    except Exception as exc:
        logger.warning("Failed to persist walk-forward run start: %s", exc)
        return uuid.uuid4().hex, None


def _persist_run_status(
    db_run_id: Optional[uuid.UUID],
    *,
    status: str,
    error_message: Optional[str] = None,
    clear_error_message: bool = False,
    started_at: Optional[datetime] = None,
) -> None:
    if db_run_id is None:
        return
    try:
        with session_scope() as session:
            update_walkforward_run(
                session,
                db_run_id,
                status=status,
                error_message=error_message,
                clear_error_message=clear_error_message,
                started_at=started_at,
            )
    except Exception as exc:
        logger.warning("Failed to persist walk-forward run status: %s", exc)


def _build_result_summary(result: WalkForwardResult) -> dict[str, Any]:
    return {
        "oos_metrics": result.oos_metrics,
        "efficiency": result.efficiency,
        "window_count": len(result.windows),
        "completed_windows": sum(
            1 for window in result.windows if window.status == "completed"
        ),
    }


def _build_windows_dataframe(windows: list[WalkForwardWindowResult]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for window in windows:
        rows.append(
            {
                "window_number": window.index,
                "train_start": window.train_start,
                "train_end": window.train_end,
                "test_start": window.test_start,
                "test_end": window.test_end,
                "status": window.status,
                "best_params_json": json.dumps(window.best_params),
                "is_metrics_json": json.dumps(window.is_metrics or {}),
                "oos_metrics_json": json.dumps(window.oos_metrics or {}),
            }
        )
    return pd.DataFrame(rows)


def _build_oos_trades_dataframe(result: WalkForwardResult) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for window in result.windows:
        for trade in window.oos_trades:
            records.append(trade.model_dump(mode="json"))
    return pd.DataFrame(records)


def _build_oos_equity_dataframe(result: WalkForwardResult) -> pd.DataFrame:
    series = result.oos_equity_curve
    return pd.DataFrame({"time": series.index, "equity": series.values})


def _write_lake_artifacts(
    run_id: str, result: WalkForwardResult
) -> Optional[dict[str, str]]:
    try:
        return write_walkforward_artifacts(
            run_id,
            _build_oos_equity_dataframe(result),
            _build_oos_trades_dataframe(result),
            _build_windows_dataframe(result.windows),
        )
    except Exception as exc:
        logger.warning("Failed to write walk-forward lake artifacts: %s", exc)
        return None


def _persist_run_finish(job: WalkForwardJob, terminal_status: JobStatus) -> None:
    if job.db_run_id is None:
        return
    result = job.result
    result_summary = _build_result_summary(result) if result is not None else None
    try:
        with session_scope() as session:
            if result is not None:
                for window in result.windows:
                    create_walkforward_window(
                        session,
                        run_id=job.db_run_id,
                        window_number=window.index,
                        train_start=_as_utc(window.train_start),
                        train_end=_as_utc(window.train_end),
                        test_start=_as_utc(window.test_start),
                        test_end=_as_utc(window.test_end),
                        status=window.status,
                        best_params=window.best_params,
                        is_metrics=window.is_metrics,
                        oos_metrics=window.oos_metrics,
                    )
            update_walkforward_run(
                session,
                job.db_run_id,
                status=terminal_status,
                result_summary=result_summary,
                lake_paths=job.lake_paths,
                error_message=job.error,
                finished_at=_now(),
            )
    except Exception as exc:
        logger.warning("Failed to persist walk-forward run finish: %s", exc)


def validate_walkforward_request(request: WalkForwardRequest) -> None:
    if request.optimization.is_multi_objective():
        raise ValueError(
            "Walk-forward analysis does not support multi-objective studies"
        )
    if request.optimization.backtest.engine == "tick":
        raise ValueError(
            "Walk-forward analysis supports candle engine only (engine='tick' "
            "is not supported yet)"
        )
    split_windows(
        request.optimization.backtest.start,
        request.optimization.backtest.end,
        request.walkforward,
    )


def start_job(
    request: WalkForwardRequest,
    backtest_runner: Optional[BacktestRunner] = None,
    market_data_service: Any | None = None,
) -> WalkForwardJob:
    validate_walkforward_request(request)

    run_id, db_run_id = _persist_run_start(request)
    job = WalkForwardJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        total_windows=len(
            split_windows(
                request.optimization.backtest.start,
                request.optimization.backtest.end,
                request.walkforward,
            )
        ),
    )

    runner = backtest_runner
    ohlcv = None
    if runner is None:
        if market_data_service is None:
            raise ValueError(
                "market_data_service is required when backtest_runner is not provided"
            )
        backtest = request.optimization.backtest
        # Load OHLCV once here (MT5 thread constraint). The frame is reused for the
        # sequential runner and shipped to worker processes for the parallel path.
        ohlcv = DefaultBacktestRunner.load_sliced_frame(
            market_data_service,
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=backtest.start,
            end=backtest.end,
        )
        runner = DefaultBacktestRunner.from_frame_sliced(ohlcv)

    # Parallelism only engages when we own the in-memory frame to ship to workers.
    if ohlcv is not None:
        job.workers = resolve_worker_count(
            request.walkforward.max_workers, job.total_windows
        )

    with _lock:
        _jobs[run_id] = job
    _persist_progress(job)
    _executor.submit(_run_job, job, runner, ohlcv)
    return job


def request_cancel(run_id: str) -> Optional[WalkForwardJob]:
    job = get_job(run_id)
    if job is not None:
        if job.status in ("pending", "running"):
            job.cancel_requested = True
            job.updated_at = _now()
            _persist_progress(job)
        return job
    # No live job. A run still marked active in the DB is an orphan left by a
    # previous process (e.g. the backend restarted mid-run): its worker thread
    # is gone, so it would stay "running" forever. Cancel it directly so the UI
    # can clear the stuck run.
    _cancel_orphaned_run(run_id)
    return None


def _cancel_orphaned_run(run_id: str) -> bool:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return False
    try:
        with session_scope() as session:
            run = get_walkforward_run(session, run_uuid)
            if run is None or run.status not in ("pending", "running"):
                return False
            update_walkforward_run(
                session,
                run_uuid,
                status="cancelled",
                error_message="Cancelled after backend restart (run was orphaned).",
                finished_at=_now(),
            )
    except Exception as exc:
        logger.warning("Failed to cancel orphaned walk-forward run %s: %s", run_id, exc)
        return False
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug("Redis progress delete unavailable for walk-forward run %s", run_id)
    return True


def reconcile_orphaned_runs() -> int:
    """Cancel runs left active by a previous process. Call once on startup.

    The in-memory job registry is empty at startup, so any run still marked
    pending/running in the DB has no live worker and will never finish.
    """
    try:
        with session_scope() as session:
            count = mark_active_runs_cancelled(
                session,
                WalkForwardRun,
                error_message="Cancelled after backend restart (run was orphaned).",
            )
    except Exception as exc:
        logger.warning("Failed to reconcile orphaned walk-forward runs: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned walk-forward run(s) on startup.", count)
    return count


def _make_progress_cb(job: WalkForwardJob) -> Callable[[WalkForwardProgress], None]:
    def _cb(progress: WalkForwardProgress) -> None:
        job.current_window = progress.current_window
        job.total_windows = progress.total_windows
        job.phase = progress.phase
        if progress.windows_completed is not None:
            # Parallel path: windows finish out of order, so trust the count.
            job.windows_completed = progress.windows_completed
        elif progress.phase == "testing":
            job.windows_completed = progress.window_index + 1
        job.updated_at = _now()
        _persist_progress(job)

    return _cb


def _run_job(
    job: WalkForwardJob,
    backtest_runner: BacktestRunner,
    ohlcv: Optional[pd.DataFrame] = None,
) -> None:
    job.status = "running"
    job.updated_at = _now()
    _persist_run_status(
        job.db_run_id,
        status=RunStatus.RUNNING.value,
        clear_error_message=True,
        started_at=_now(),
    )
    _persist_progress(job)

    terminal_status: JobStatus = "failed"
    try:
        wf_runner = WalkForwardRunner(
            job.request.optimization,
            job.request.walkforward,
            backtest_runner,
            ohlcv=ohlcv,
        )
        job.result = wf_runner.run(
            progress_callback=_make_progress_cb(job),
            should_stop=lambda: job.cancel_requested,
        )
        job.lake_paths = _write_lake_artifacts(job.run_id, job.result)
        if job.cancel_requested:
            terminal_status = "cancelled"
            job.error = "Cancelled by user"
        else:
            terminal_status = "completed"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Walk-forward job %s failed", job.run_id)
        terminal_status = "failed"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        # Persist to the DB *before* flipping the in-memory status to terminal, so
        # a watcher that observes job.status == terminal (e.g. after a restart and
        # cache clear) never reads a stale "running" row from the database.
        _persist_run_finish(job, terminal_status)
        job.status = terminal_status
        _persist_progress(job)


def status_payload(job: WalkForwardJob) -> dict[str, Any]:
    optimization_config = job.request.optimization.model_dump(mode="json")
    walkforward_config = job.request.walkforward.model_dump(mode="json")
    return {
        "run_id": job.run_id,
        "status": job.status,
        "current_window": job.current_window,
        "total_windows": job.total_windows,
        "workers": job.workers,
        "phase": job.phase,
        "windows_completed": job.windows_completed,
        "error": job.error,
        "optimization_config": optimization_config,
        "walkforward_config": walkforward_config,
        "backtest_config": optimization_config.get("backtest"),
    }


def _serialize_window(window: WalkForwardWindowResult) -> dict[str, Any]:
    return {
        "index": window.index,
        "train_start": _isoformat(window.train_start),
        "train_end": _isoformat(window.train_end),
        "test_start": _isoformat(window.test_start),
        "test_end": _isoformat(window.test_end),
        "status": window.status,
        "best_params": window.best_params,
        "is_metrics": window.is_metrics,
        "oos_metrics": window.oos_metrics,
    }


def serialize_equity_points(series: pd.Series) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for timestamp, equity in series.items():
        ts = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else timestamp
        points.append({"time": _isoformat(ts), "equity": float(equity)})
    return points


def results_payload(job: WalkForwardJob) -> Optional[dict[str, Any]]:
    result = job.result
    if result is None:
        return None
    optimization_config = job.request.optimization.model_dump(mode="json")
    return {
        "run_id": job.run_id,
        "status": job.status,
        "windows": [_serialize_window(window) for window in result.windows],
        "oos_metrics": result.oos_metrics,
        "efficiency": result.efficiency,
        "equity_curve": serialize_equity_points(result.oos_equity_curve),
        "optimization_config": optimization_config,
        "walkforward_config": job.request.walkforward.model_dump(mode="json"),
        "lake_paths": job.lake_paths,
    }


def status_payload_from_db(run_id: str) -> dict[str, Any] | None:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            run = get_walkforward_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load walk-forward run %s from DB: %s", run_id, exc)
        return None

    if run is None:
        return None

    config = run.config or {}
    optimization_config = config.get("optimization")
    walkforward_config = config.get("walkforward")
    windows = sorted(run.windows, key=lambda item: item.window_number)
    completed_windows = sum(1 for window in windows if window.status == "completed")

    return {
        "run_id": run_id,
        "status": run.status,
        "current_window": len(windows),
        "total_windows": len(windows),
        "phase": None,
        "windows_completed": completed_windows,
        "error": run.error_message,
        "optimization_config": optimization_config,
        "walkforward_config": walkforward_config,
        "backtest_config": (
            optimization_config.get("backtest") if optimization_config else None
        ),
    }


def get_status_payload(run_id: str) -> dict[str, Any] | None:
    job = get_job(run_id)
    if job is not None:
        return status_payload(job)

    db_payload = status_payload_from_db(run_id)

    try:
        cached = get_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
        if cached is not None and db_payload is None:
            return cached
    except Exception:
        logger.debug("Redis progress read unavailable for walk-forward run %s", run_id)

    return db_payload


def get_persisted_run_status(run_id: str) -> Optional[str]:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            run = get_walkforward_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load walk-forward run %s from DB: %s", run_id, exc)
        return None
    return run.status if run is not None else None


def _load_equity_curve_from_lake(run_id: str) -> list[dict[str, Any]]:
    df = read_walkforward_artifact(run_id, "oos_equity")
    time_col = "time" if "time" in df.columns else df.columns[0]
    equity_col = "equity" if "equity" in df.columns else df.columns[1]
    points: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        ts = row[time_col]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        points.append({"time": _isoformat(ts), "equity": float(row[equity_col])})
    return points


def _serialize_db_window(window) -> dict[str, Any]:
    return {
        "index": window.window_number,
        "train_start": _isoformat(window.train_start),
        "train_end": _isoformat(window.train_end),
        "test_start": _isoformat(window.test_start),
        "test_end": _isoformat(window.test_end),
        "status": window.status,
        "best_params": window.best_params,
        "is_metrics": window.is_metrics,
        "oos_metrics": window.oos_metrics,
    }


def results_payload_from_db(run_id: str) -> Optional[dict[str, Any]]:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            run = get_walkforward_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load walk-forward run %s from DB: %s", run_id, exc)
        return None

    if run is None:
        return None

    if run.status not in _FINISHED_STATUSES:
        return None

    config = run.config or {}
    equity_curve: list[dict[str, Any]] = []
    if run.lake_paths and run.lake_paths.get("oos_equity"):
        try:
            equity_curve = _load_equity_curve_from_lake(run_id)
        except Exception as exc:
            logger.warning("Failed to load walk-forward equity from lake: %s", exc)

    summary = run.result_summary or {}
    windows = sorted(run.windows, key=lambda item: item.window_number)

    return {
        "run_id": run_id,
        "status": run.status,
        "windows": [_serialize_db_window(window) for window in windows],
        "oos_metrics": summary.get("oos_metrics", {}),
        "efficiency": summary.get("efficiency"),
        "equity_curve": equity_curve,
        "optimization_config": config.get("optimization"),
        "walkforward_config": config.get("walkforward"),
        "lake_paths": run.lake_paths,
    }


def run_list_item_from_db(run) -> dict[str, Any]:
    summary = run.result_summary or {}
    config = run.config or {}
    optimization = config.get("optimization", {})
    backtest = optimization.get("backtest", {})
    return {
        "run_id": run.id.hex,
        "name": run.name,
        "status": run.status,
        "symbol": backtest.get("symbol"),
        "strategy": backtest.get("strategy"),
        "efficiency": summary.get("efficiency"),
        "window_count": summary.get("window_count", len(run.windows)),
        "created_at": run.created_at,
    }


def delete_run_lake_artifacts(run_id: str) -> None:
    try:
        delete_walkforward_artifacts(run_id)
    except Exception as exc:
        logger.warning("Failed to delete walk-forward lake artifacts for %s: %s", run_id, exc)
