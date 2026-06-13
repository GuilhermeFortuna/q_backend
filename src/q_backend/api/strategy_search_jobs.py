"""Job manager for asynchronous strategy search runs.

Strategy searches are long and serialized via a single-worker thread pool.
Progress snapshots are mirrored to Redis under the ``strategy_search`` namespace;
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

from q_backend.optimization.backtest_runner import BacktestRunner, DefaultBacktestRunner
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    RegistryCandidateProvider,
    SearchProgress,
    StrategySearchConfig,
    StrategySearchResult,
    StrategySearchRunner,
)
from q_backend.optimization.walkforward import split_windows
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import RunStatus, StrategySearchRun
from q_backend.storage.db.repositories import (
    create_strategy_search_candidate,
    create_strategy_search_run,
    get_strategy_search_run,
    mark_active_runs_cancelled,
    update_strategy_search_run,
)
from q_backend.storage.lake.artifacts import (
    delete_strategy_search_artifacts,
    read_strategy_search_candidate_artifact,
    write_strategy_search_artifacts,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    delete_job_progress,
    get_job_progress,
    set_job_progress,
)

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "strategy_search"

StrategySearchRequest = StrategySearchConfig

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

_FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled"})


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
class StrategySearchJob:
    run_id: str
    request: StrategySearchConfig
    db_run_id: Optional[uuid.UUID] = None
    status: JobStatus = "pending"
    current_candidate: int = 0
    total_candidates: int = 0
    candidate_id: Optional[str] = None
    strategy: Optional[str] = None
    phase: Optional[Literal["optimizing", "testing", "done"]] = None
    window_index: Optional[int] = None
    total_windows: Optional[int] = None
    result: Optional[StrategySearchResult] = None
    lake_paths: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


_jobs: dict[str, StrategySearchJob] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy_search")


def get_job(run_id: str) -> Optional[StrategySearchJob]:
    with _lock:
        return _jobs.get(run_id)


def evict_run(run_id: str) -> None:
    with _lock:
        _jobs.pop(run_id, None)
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug("Redis progress delete unavailable for strategy search run %s", run_id)


def validate_strategy_search_request(config: StrategySearchConfig) -> None:
    split_windows(config.backtest.start, config.backtest.end, config.walkforward)


def _request_config_payload(config: StrategySearchConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def _persist_progress(job: StrategySearchJob) -> None:
    try:
        set_job_progress(
            get_redis(),
            job.run_id,
            status_payload(job),
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:
        logger.debug("Redis progress unavailable for strategy search run %s", job.run_id)


def _persist_run_start(config: StrategySearchConfig) -> tuple[str, Optional[uuid.UUID]]:
    try:
        with session_scope() as session:
            run = create_strategy_search_run(
                session,
                name=config.study.name,
                config=_request_config_payload(config),
                status=RunStatus.PENDING.value,
            )
            return run.id.hex, run.id
    except Exception as exc:
        logger.warning("Failed to persist strategy search run start: %s", exc)
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
            update_strategy_search_run(
                session,
                db_run_id,
                status=status,
                error_message=error_message,
                clear_error_message=clear_error_message,
                started_at=started_at,
            )
    except Exception as exc:
        logger.warning("Failed to persist strategy search run status: %s", exc)


def _build_result_summary(result: StrategySearchResult) -> dict[str, Any]:
    best = result.best
    return {
        "objective_mode": result.objective_mode.value,
        "candidate_count": len(result.candidates),
        "ranked_count": sum(1 for candidate in result.candidates if candidate.rank is not None),
        "passed_gates_count": sum(
            1 for candidate in result.candidates if candidate.passed_gates
        ),
        "best_candidate_id": best.candidate_id if best is not None else None,
        "best_strategy": best.strategy if best is not None else None,
        "best_objective_value": best.objective_value if best is not None else None,
        "best_efficiency": best.efficiency if best is not None else None,
    }


def _build_leaderboard_dataframe(candidates: list[CandidateResult]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "strategy": candidate.strategy,
                "status": candidate.status,
                "rank": candidate.rank,
                "objective_value": candidate.objective_value,
                "robustness_score": candidate.robustness_score,
                "efficiency": candidate.efficiency,
                "passed_gates": candidate.passed_gates,
                "gate_flags_json": json.dumps(candidate.gate_flags),
                "window_count": candidate.window_count,
                "completed_windows": candidate.completed_windows,
                "oos_metrics_json": json.dumps(candidate.oos_metrics or {}),
                "best_params_json": json.dumps(candidate.best_params or {}),
            }
        )
    return pd.DataFrame(rows)


def _build_candidate_equity_dataframes(
    candidates: list[CandidateResult],
) -> dict[str, pd.DataFrame]:
    equity: dict[str, pd.DataFrame] = {}
    for candidate in candidates:
        if candidate.oos_equity_curve is None:
            continue
        series = candidate.oos_equity_curve
        equity[candidate.candidate_id] = pd.DataFrame(
            {"time": series.index, "equity": series.values}
        )
    return equity


def _write_lake_artifacts(
    run_id: str, result: StrategySearchResult
) -> Optional[dict[str, Any]]:
    try:
        return write_strategy_search_artifacts(
            run_id,
            _build_leaderboard_dataframe(result.candidates),
            _build_candidate_equity_dataframes(result.candidates),
        )
    except Exception as exc:
        logger.warning("Failed to write strategy search lake artifacts: %s", exc)
        return None


def _persist_run_finish(job: StrategySearchJob, terminal_status: JobStatus) -> None:
    if job.db_run_id is None:
        return
    result = job.result
    result_summary = _build_result_summary(result) if result is not None else None
    try:
        with session_scope() as session:
            if result is not None:
                for candidate in result.candidates:
                    create_strategy_search_candidate(
                        session,
                        run_id=job.db_run_id,
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        status=candidate.status,
                        rank=candidate.rank,
                        objective_value=candidate.objective_value,
                        robustness_score=candidate.robustness_score,
                        efficiency=candidate.efficiency,
                        gate_flags=list(candidate.gate_flags),
                        passed_gates=candidate.passed_gates,
                        oos_metrics=candidate.oos_metrics,
                        is_metrics_summary=candidate.is_metrics_summary,
                        best_params=candidate.best_params,
                        window_count=candidate.window_count,
                        completed_windows=candidate.completed_windows,
                    )
            update_strategy_search_run(
                session,
                job.db_run_id,
                status=terminal_status,
                result_summary=result_summary,
                lake_paths=job.lake_paths,
                error_message=job.error,
                finished_at=_now(),
            )
    except Exception as exc:
        logger.warning("Failed to persist strategy search run finish: %s", exc)


def _resolve_total_candidates(
    config: StrategySearchConfig,
    provider: CandidateProvider,
) -> int:
    if isinstance(provider, RegistryCandidateProvider):
        candidates = list(provider.candidates())
        return len(candidates) + len(provider.unsupported_names())
    return len(list(provider.candidates()))


def start_job(
    request: StrategySearchConfig,
    backtest_runner: Optional[BacktestRunner] = None,
    market_data_service: Any | None = None,
    provider: CandidateProvider | None = None,
) -> StrategySearchJob:
    validate_strategy_search_request(request)

    run_id, db_run_id = _persist_run_start(request)
    resolved_provider = provider or RegistryCandidateProvider(request)
    job = StrategySearchJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        total_candidates=_resolve_total_candidates(request, resolved_provider),
    )

    runner = backtest_runner
    if runner is None:
        if market_data_service is None:
            raise ValueError(
                "market_data_service is required when backtest_runner is not provided"
            )
        backtest = request.backtest
        runner = DefaultBacktestRunner.from_market_data_sliced(
            market_data_service,
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=backtest.start,
            end=backtest.end,
        )

    with _lock:
        _jobs[run_id] = job
    _persist_progress(job)
    _executor.submit(_run_job, job, runner, resolved_provider)
    return job


def request_cancel(run_id: str) -> Optional[StrategySearchJob]:
    job = get_job(run_id)
    if job is not None:
        if job.status in ("pending", "running"):
            job.cancel_requested = True
            job.updated_at = _now()
            _persist_progress(job)
        return job
    # No live job. A run still marked active in the DB is an orphan left by a
    # previous process (e.g. the backend restarted mid-search): its worker
    # thread is gone, so it would stay "running" forever. Cancel it directly
    # so the UI can clear the stuck run.
    _cancel_orphaned_run(run_id)
    return None


def _cancel_orphaned_run(run_id: str) -> bool:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return False
    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
            if run is None or run.status not in ("pending", "running"):
                return False
            update_strategy_search_run(
                session,
                run_uuid,
                status="cancelled",
                error_message="Cancelled after backend restart (run was orphaned).",
                finished_at=_now(),
            )
    except Exception as exc:
        logger.warning("Failed to cancel orphaned strategy search run %s: %s", run_id, exc)
        return False
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug("Redis progress delete unavailable for strategy search run %s", run_id)
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
                StrategySearchRun,
                error_message="Cancelled after backend restart (run was orphaned).",
            )
    except Exception as exc:
        logger.warning("Failed to reconcile orphaned strategy search runs: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned strategy search run(s) on startup.", count)
    return count


def _make_progress_cb(job: StrategySearchJob) -> Callable[[SearchProgress], None]:
    def _cb(progress: SearchProgress) -> None:
        job.current_candidate = progress.current_candidate
        job.total_candidates = progress.total_candidates
        job.candidate_id = progress.candidate_id
        job.strategy = progress.strategy
        job.phase = progress.phase
        job.window_index = progress.window_index
        job.total_windows = progress.total_windows
        job.updated_at = _now()
        _persist_progress(job)

    return _cb


def _run_job(
    job: StrategySearchJob,
    backtest_runner: BacktestRunner,
    provider: CandidateProvider,
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
        job.result = StrategySearchRunner(
            job.request,
            backtest_runner,
            provider=provider,
        ).run(
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
        logger.exception("Strategy search job %s failed", job.run_id)
        terminal_status = "failed"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        _persist_run_finish(job, terminal_status)
        job.status = terminal_status
        _persist_progress(job)


def status_payload(job: StrategySearchJob) -> dict[str, Any]:
    config = job.request.model_dump(mode="json")
    return {
        "run_id": job.run_id,
        "status": job.status,
        "current_candidate": job.current_candidate,
        "total_candidates": job.total_candidates,
        "candidate_id": job.candidate_id,
        "strategy": job.strategy,
        "phase": job.phase,
        "window_index": job.window_index,
        "total_windows": job.total_windows,
        "error": job.error,
        "search_config": config,
        "backtest_config": config.get("backtest"),
    }


def _serialize_candidate(candidate: CandidateResult) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "strategy": candidate.strategy,
        "status": candidate.status,
        "rank": candidate.rank,
        "objective_value": candidate.objective_value,
        "robustness_score": candidate.robustness_score,
        "efficiency": candidate.efficiency,
        "gate_flags": list(candidate.gate_flags),
        "passed_gates": candidate.passed_gates,
        "oos_metrics": candidate.oos_metrics,
        "is_metrics_summary": candidate.is_metrics_summary,
        "best_params": candidate.best_params,
        "window_count": candidate.window_count,
        "completed_windows": candidate.completed_windows,
        "error": candidate.error,
    }


def serialize_equity_points(series: pd.Series) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for timestamp, equity in series.items():
        ts = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else timestamp
        points.append({"time": _isoformat(ts), "equity": float(equity)})
    return points


def results_payload(job: StrategySearchJob) -> Optional[dict[str, Any]]:
    result = job.result
    if result is None:
        return None
    summary = _build_result_summary(result)
    return {
        "run_id": job.run_id,
        "status": job.status,
        "objective_mode": result.objective_mode.value,
        "summary": summary,
        "candidates": [_serialize_candidate(candidate) for candidate in result.candidates],
        "best": _serialize_candidate(result.best) if result.best is not None else None,
        "search_config": job.request.model_dump(mode="json"),
        "lake_paths": job.lake_paths,
    }


def _serialize_db_candidate(candidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "strategy": candidate.strategy,
        "status": candidate.status,
        "rank": candidate.rank,
        "objective_value": candidate.objective_value,
        "robustness_score": candidate.robustness_score,
        "efficiency": candidate.efficiency,
        "gate_flags": candidate.gate_flags or [],
        "passed_gates": candidate.passed_gates,
        "oos_metrics": candidate.oos_metrics,
        "is_metrics_summary": candidate.is_metrics_summary,
        "best_params": candidate.best_params,
        "window_count": candidate.window_count,
        "completed_windows": candidate.completed_windows,
        "error": None,
    }


def status_payload_from_db(run_id: str) -> dict[str, Any] | None:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load strategy search run %s from DB: %s", run_id, exc)
        return None

    if run is None:
        return None

    config = run.config or {}
    candidates = sorted(run.candidates, key=lambda item: item.candidate_id)

    return {
        "run_id": run_id,
        "status": run.status,
        "current_candidate": len(candidates),
        "total_candidates": len(candidates),
        "candidate_id": candidates[-1].candidate_id if candidates else None,
        "strategy": candidates[-1].strategy if candidates else None,
        "phase": None,
        "window_index": None,
        "total_windows": None,
        "error": run.error_message,
        "search_config": config,
        "backtest_config": config.get("backtest"),
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
        logger.debug("Redis progress read unavailable for strategy search run %s", run_id)

    return db_payload


def get_persisted_run_status(run_id: str) -> Optional[str]:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load strategy search run %s from DB: %s", run_id, exc)
        return None
    return run.status if run is not None else None


def _load_equity_curve_from_lake(run_id: str, candidate_id: str) -> list[dict[str, Any]]:
    df = read_strategy_search_candidate_artifact(run_id, candidate_id, "oos_equity")
    time_col = "time" if "time" in df.columns else df.columns[0]
    equity_col = "equity" if "equity" in df.columns else df.columns[1]
    points: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        ts = row[time_col]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        points.append({"time": _isoformat(ts), "equity": float(row[equity_col])})
    return points


def results_payload_from_db(run_id: str) -> Optional[dict[str, Any]]:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception as exc:
        logger.warning("Failed to load strategy search run %s from DB: %s", run_id, exc)
        return None

    if run is None:
        return None

    if run.status not in _FINISHED_STATUSES:
        return None

    candidates = sorted(run.candidates, key=lambda item: (item.rank or 10_000, item.candidate_id))
    serialized = [_serialize_db_candidate(candidate) for candidate in candidates]
    best = next((item for item in serialized if item.get("rank") == 1), None)

    return {
        "run_id": run_id,
        "status": run.status,
        "objective_mode": (run.result_summary or {}).get("objective_mode"),
        "summary": run.result_summary or {},
        "candidates": serialized,
        "best": best,
        "search_config": run.config,
        "lake_paths": run.lake_paths,
    }


def run_list_item_from_db(run) -> dict[str, Any]:
    summary = run.result_summary or {}
    config = run.config or {}
    backtest = config.get("backtest", {})
    return {
        "run_id": run.id.hex,
        "name": run.name,
        "status": run.status,
        "symbol": backtest.get("symbol"),
        "candidate_count": summary.get("candidate_count", len(run.candidates)),
        "best_strategy": summary.get("best_strategy"),
        "best_objective_value": summary.get("best_objective_value"),
        "created_at": run.created_at,
    }


def delete_run_lake_artifacts(run_id: str) -> None:
    try:
        delete_strategy_search_artifacts(run_id)
    except Exception as exc:
        logger.warning(
            "Failed to delete strategy search lake artifacts for %s: %s", run_id, exc
        )


def candidate_exists_in_run(run_id: str, candidate_id: str) -> bool:
    job = get_job(run_id)
    if job is not None and job.result is not None:
        return any(
            candidate.candidate_id == candidate_id for candidate in job.result.candidates
        )

    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return False

    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception:
        return False

    if run is None:
        return False
    return any(candidate.candidate_id == candidate_id for candidate in run.candidates)
