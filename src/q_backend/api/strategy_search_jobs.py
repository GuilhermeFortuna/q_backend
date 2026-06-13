"""Job manager for asynchronous strategy search runs.

Searches are dispatched to the Dramatiq worker pool: a coordinator fans each
candidate out as its own worker message, the last to finish ranks them and runs the
finalizer. Progress is read from the database and Redis — no per-run state lives in
the API process.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import pandas as pd

from q_backend.optimization.backtest_runner import BacktestRunner, DefaultBacktestRunner
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    RegistryCandidateProvider,
    StrategySearchConfig,
    StrategySearchResult,
    _rank_results,
    _unsupported_result,
    evaluate_candidate,
)
from q_backend.optimization.walkforward import split_windows
from q_backend.tasks.data import load_ohlcv_frame
from q_backend.tasks.fanin import (
    clear_job_keys,
    decrement,
    init_counter,
    is_cancelled,
    set_cancelled,
)
from q_backend.tasks.serialization import (
    candidate_result_from_dict,
    candidate_result_to_dict,
)
from q_backend.tasks.staging import clear_partials, load_partials, stash_partial
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


def get_job(run_id: str) -> Optional[StrategySearchJob]:
    """Runs no longer live in the API process; status is read from DB/Redis."""
    return None


def evict_run(run_id: str) -> None:
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug(
            "Redis progress delete unavailable for strategy search run %s", run_id
        )
    clear_partials(run_id)
    clear_job_keys(run_id)


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
        logger.debug(
            "Redis progress unavailable for strategy search run %s", job.run_id
        )


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
        "ranked_count": sum(
            1 for candidate in result.candidates if candidate.rank is not None
        ),
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
    """Persist the run and dispatch it to the worker pool.

    Candidate workers fetch their own market data (cached in the lake), so the
    ``backtest_runner``/``market_data_service``/``provider`` arguments are retained
    only for call-site compatibility.
    """
    validate_strategy_search_request(request)

    run_id, db_run_id = _persist_run_start(request)
    job = StrategySearchJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        total_candidates=_resolve_total_candidates(
            request, RegistryCandidateProvider(request)
        ),
    )
    _persist_progress(job)

    from q_backend.tasks import actors

    actors.discovery_coordinator.send(
        run_id,
        db_run_id.hex if db_run_id is not None else "",
        request.model_dump_json(),
    )
    return job


def request_cancel(run_id: str) -> Optional[StrategySearchJob]:
    # Raise the cancel flag so in-flight candidate workers stop and pending
    # candidate messages drain as no-ops, then flip the DB/Redis state immediately
    # so the UI clears (and an orphaned run from a previous process is resolved).
    set_cancelled(run_id)
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
        logger.warning(
            "Failed to cancel orphaned strategy search run %s: %s", run_id, exc
        )
        return False
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug(
            "Redis progress delete unavailable for strategy search run %s", run_id
        )
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


# Stash key offset for unsupported candidates so they never collide with the
# 0..N-1 indices used for evaluated candidates.
_UNSUPPORTED_OFFSET = 10_000_000


def _candidate_runner(request: StrategySearchConfig) -> DefaultBacktestRunner:
    """Build a runner that slices the run's cached OHLCV frame per window."""
    backtest = request.backtest
    return DefaultBacktestRunner.from_frame_sliced(
        load_ohlcv_frame(
            backtest.symbol, backtest.timeframe, backtest.start, backtest.end
        )
    )


def _progress_job(
    run_id: str,
    db_run_id: Optional[uuid.UUID],
    request: StrategySearchConfig,
    *,
    status: JobStatus,
    total_candidates: int,
    current_candidate: int,
    candidate_id: Optional[str] = None,
    strategy: Optional[str] = None,
    phase: Optional[Literal["optimizing", "testing", "done"]] = None,
    error: Optional[str] = None,
) -> StrategySearchJob:
    return StrategySearchJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        status=status,
        total_candidates=total_candidates,
        current_candidate=current_candidate,
        candidate_id=candidate_id,
        strategy=strategy,
        phase=phase,
        error=error,
    )


def dispatch_candidates(run_id: str, db_run_id_hex: str, config_json: str) -> None:
    """Coordinator: fan each candidate out to its own worker message."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    provider = RegistryCandidateProvider(request)
    candidates = list(provider.candidates())
    unsupported = [_unsupported_result(name) for name in provider.unsupported_names()]
    total = len(candidates) + len(unsupported)

    if is_cancelled(run_id):
        finalize_discovery(run_id, db_run_id_hex, config_json)
        return

    _persist_run_status(
        db_run_id,
        status=RunStatus.RUNNING.value,
        clear_error_message=True,
        started_at=_now(),
    )
    # Unsupported candidates need no evaluation — stash them up front.
    for offset, result in enumerate(unsupported):
        stash_partial(
            run_id, _UNSUPPORTED_OFFSET + offset, candidate_result_to_dict(result)
        )

    _persist_progress(
        _progress_job(
            run_id,
            db_run_id,
            request,
            status="running",
            total_candidates=total,
            current_candidate=len(unsupported),
            phase="optimizing",
        )
    )

    if not candidates:
        finalize_discovery(run_id, db_run_id_hex, config_json)
        return

    init_counter(run_id, len(candidates))

    from q_backend.tasks import actors

    for index in range(len(candidates)):
        actors.evaluate_discovery_candidate.send(
            run_id, db_run_id_hex, config_json, index
        )


def run_candidate(
    run_id: str, db_run_id_hex: str, config_json: str, candidate_index: int
) -> None:
    """Candidate worker: walk-forward evaluate one candidate strategy."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    provider = RegistryCandidateProvider(request)
    candidates = list(provider.candidates())
    candidate = candidates[candidate_index]

    if not is_cancelled(run_id):
        try:
            result = evaluate_candidate(candidate, request, _candidate_runner(request))
        except Exception as exc:  # noqa: BLE001 - isolate a single candidate failure
            logger.exception(
                "Discovery candidate %s failed for run %s",
                candidate.candidate_id,
                run_id,
            )
            result = CandidateResult(
                candidate_id=candidate.candidate_id,
                strategy=candidate.strategy,
                status="error",
                error=str(exc),
            )
        stash_partial(run_id, candidate_index, candidate_result_to_dict(result))

    remaining = decrement(run_id)
    unsupported_count = len(provider.unsupported_names())
    completed = unsupported_count + (len(candidates) - max(remaining, 0))
    _persist_progress(
        _progress_job(
            run_id,
            db_run_id,
            request,
            status="running",
            total_candidates=len(candidates) + unsupported_count,
            current_candidate=completed,
            candidate_id=candidate.candidate_id,
            strategy=candidate.strategy,
            phase="testing",
        )
    )
    if remaining <= 0:
        finalize_discovery(run_id, db_run_id_hex, config_json)


def finalize_discovery(run_id: str, db_run_id_hex: str, config_json: str) -> None:
    """Last candidate worker: rank candidates, persist results, mark terminal."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    job = StrategySearchJob(run_id=run_id, db_run_id=db_run_id, request=request)

    terminal_status: JobStatus = "failed"
    try:
        results = [candidate_result_from_dict(p) for p in load_partials(run_id)]
        ranked = _rank_results(results)
        best = ranked[0] if ranked and ranked[0].rank == 1 else None
        job.result = StrategySearchResult(
            candidates=ranked,
            objective_mode=request.objective.mode,
            best=best,
        )
        job.total_candidates = len(ranked)
        job.lake_paths = _write_lake_artifacts(run_id, job.result)
        if is_cancelled(run_id):
            terminal_status = "cancelled"
            job.error = "Cancelled by user"
        else:
            terminal_status = "completed"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Discovery finalize failed for run %s", run_id)
        terminal_status = "failed"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        _persist_run_finish(job, terminal_status)
        job.status = terminal_status
        _persist_progress(job)
        clear_partials(run_id)
        clear_job_keys(run_id)


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
        ts = (
            timestamp.to_pydatetime()
            if isinstance(timestamp, pd.Timestamp)
            else timestamp
        )
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
        "candidates": [
            _serialize_candidate(candidate) for candidate in result.candidates
        ],
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
    db_payload = status_payload_from_db(run_id)

    try:
        cached = get_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:
        logger.debug(
            "Redis progress read unavailable for strategy search run %s", run_id
        )
        cached = None

    # While the run is still active, candidate results aren't in the DB yet (they
    # are written by the finalizer), so the live Redis snapshot is authoritative.
    # Once terminal, the DB row is complete and wins.
    if db_payload is None:
        return cached
    if db_payload.get("status") in ("pending", "running") and cached is not None:
        return cached
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


def _load_equity_curve_from_lake(
    run_id: str, candidate_id: str
) -> list[dict[str, Any]]:
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

    candidates = sorted(
        run.candidates, key=lambda item: (item.rank or 10_000, item.candidate_id)
    )
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
            candidate.candidate_id == candidate_id
            for candidate in job.result.candidates
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
