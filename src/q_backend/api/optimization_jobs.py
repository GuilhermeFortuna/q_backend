"""Job manager for asynchronous Optuna optimization runs.

Studies run on background worker threads. Progress snapshots are mirrored to
Redis when available; execution state and full results remain in-memory.
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

import optuna

from q_backend.optimization import (
    BacktestRunner,
    DefaultBacktestRunner,
    OptimizationConfig,
    OptimizationResult,
    OptimizationRunner,
    serialize_trial,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import RunStatus, TrialStatus
from q_backend.storage.db.repositories import (
    create_optimization_study,
    create_optimization_trial,
    get_optimization_study,
    get_optimization_trial_by_number,
    update_optimization_study,
    update_optimization_trial,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import get_job_progress, set_job_progress

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "done", "error", "cancelled"]

STATUS_PAYLOAD_KEYS = frozenset(
    {
        "study_id",
        "status",
        "completed_trials",
        "n_trials",
        "best_value",
        "best_params",
        "error",
    }
)

_FINISHED_TRIAL_STATUSES = frozenset(
    {
        TrialStatus.COMPLETED.value,
        TrialStatus.FAILED.value,
        TrialStatus.PRUNED.value,
    }
)

_OPTUNA_STATE_TO_TRIAL_STATUS = {
    optuna.trial.TrialState.COMPLETE: TrialStatus.COMPLETED.value,
    optuna.trial.TrialState.FAIL: TrialStatus.FAILED.value,
    optuna.trial.TrialState.PRUNED: TrialStatus.PRUNED.value,
    optuna.trial.TrialState.RUNNING: TrialStatus.RUNNING.value,
    optuna.trial.TrialState.WAITING: TrialStatus.PENDING.value,
}

_TRIAL_STATUS_TO_OPTUNA_STATE = {
    TrialStatus.COMPLETED.value: "COMPLETE",
    TrialStatus.FAILED.value: "FAIL",
    TrialStatus.PRUNED.value: "PRUNED",
    TrialStatus.RUNNING.value: "RUNNING",
    TrialStatus.PENDING.value: "WAITING",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_study_uuid(study_id: str) -> uuid.UUID:
    if len(study_id) == 32:
        return uuid.UUID(hex=study_id)
    return uuid.UUID(study_id)


@dataclass
class OptimizationJob:
    study_id: str
    config: OptimizationConfig
    n_trials: int
    db_study_id: Optional[uuid.UUID] = None
    status: JobStatus = "pending"
    completed_trials: int = 0
    best_value: Optional[float] = None
    best_params: dict[str, Any] = field(default_factory=dict)
    result: Optional[OptimizationResult] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


_jobs: dict[str, OptimizationJob] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="optimize")


def get_job(study_id: str) -> Optional[OptimizationJob]:
    with _lock:
        return _jobs.get(study_id)


def _persist_progress(job: OptimizationJob) -> None:
    try:
        set_job_progress(get_redis(), job.study_id, status_payload(job))
    except Exception:
        logger.debug("Redis progress unavailable for study %s", job.study_id)


def _trial_metrics(trial: optuna.trial.FrozenTrial) -> dict[str, Any]:
    return {
        "values": trial.values,
        "user_attrs": dict(trial.user_attrs),
        "state": trial.state.name,
    }


def _trial_status(trial: optuna.trial.FrozenTrial) -> str:
    return _OPTUNA_STATE_TO_TRIAL_STATUS.get(
        trial.state, TrialStatus.PENDING.value
    )


def _persist_study_start(
    config: OptimizationConfig,
) -> tuple[str, Optional[uuid.UUID]]:
    try:
        with session_scope() as session:
            study = create_optimization_study(
                session,
                name=config.study.name,
                config=config.model_dump(mode="json"),
                status=RunStatus.PENDING.value,
            )
            return study.id.hex, study.id
    except Exception as exc:
        logger.warning("Failed to persist optimization study start: %s", exc)
        return uuid.uuid4().hex, None


def _persist_study_status(
    db_study_id: Optional[uuid.UUID],
    *,
    status: str,
    config: Optional[dict[str, Any]] = None,
) -> None:
    if db_study_id is None:
        return
    try:
        with session_scope() as session:
            update_optimization_study(
                session,
                db_study_id,
                status=status,
                config=config,
            )
    except Exception as exc:
        logger.warning("Failed to persist optimization study status: %s", exc)


def _persist_trial(
    db_study_id: Optional[uuid.UUID],
    trial: optuna.trial.FrozenTrial,
) -> None:
    if db_study_id is None:
        return
    try:
        with session_scope() as session:
            _upsert_trial_in_session(session, db_study_id, trial)
    except Exception as exc:
        logger.warning(
            "Failed to persist optimization trial %s for study %s: %s",
            trial.number,
            db_study_id,
            exc,
        )


def _upsert_trial_in_session(
    session,
    db_study_id: uuid.UUID,
    trial: optuna.trial.FrozenTrial,
) -> None:
    existing = get_optimization_trial_by_number(
        session,
        study_id=db_study_id,
        trial_number=trial.number,
    )
    status = _trial_status(trial)
    metrics = _trial_metrics(trial)
    if existing is None:
        create_optimization_trial(
            session,
            study_id=db_study_id,
            trial_number=trial.number,
            params=dict(trial.params),
            status=status,
            metrics=metrics,
        )
    else:
        update_optimization_trial(
            session,
            existing.id,
            status=status,
            params=dict(trial.params),
            metrics=metrics,
        )


def _persist_study_finish(job: OptimizationJob) -> None:
    if job.db_study_id is None:
        return
    result = job.result
    snapshot: dict[str, Any] = {
        "best_params": job.best_params,
        "best_value": job.best_value,
        "failures": result.failures if result is not None else [],
        "error": job.error,
    }
    if result is not None and result.best_trial is not None:
        snapshot["best_trial_number"] = result.best_trial.number
    if result is not None:
        snapshot["pareto_trial_numbers"] = [t.number for t in result.pareto_trials]

    config = job.config.model_dump(mode="json")
    config["persisted_snapshot"] = snapshot

    try:
        with session_scope() as session:
            if result is not None:
                for trial in result.study.trials:
                    if trial.state.is_finished():
                        _upsert_trial_in_session(session, job.db_study_id, trial)
            update_optimization_study(
                session,
                job.db_study_id,
                status=job.status,
                config=config,
            )
    except Exception as exc:
        logger.warning("Failed to persist optimization study finish: %s", exc)


def _serialize_db_trial(trial_metrics: dict[str, Any] | None, trial) -> dict[str, Any]:
    metrics = trial_metrics or {}
    return {
        "number": trial.trial_number,
        "params": trial.params,
        "values": metrics.get("values"),
        "user_attrs": metrics.get("user_attrs", {}),
        "state": metrics.get("state")
        or _TRIAL_STATUS_TO_OPTUNA_STATE.get(trial.status, "WAITING"),
    }


def _count_completed_trials(trials: list) -> int:
    return sum(1 for trial in trials if trial.status in _FINISHED_TRIAL_STATUSES)


def get_status_payload(study_id: str) -> dict[str, Any] | None:
    try:
        cached = get_job_progress(get_redis(), study_id)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("Redis progress read unavailable for study %s", study_id)
    job = get_job(study_id)
    return status_payload(job) if job else None


def start_job(
    config: OptimizationConfig,
    backtest_runner: Optional[BacktestRunner] = None,
    market_data_service: Any | None = None,
) -> OptimizationJob:
    study_id, db_study_id = _persist_study_start(config)
    job = OptimizationJob(
        study_id=study_id,
        db_study_id=db_study_id,
        config=config,
        n_trials=config.study.n_trials,
    )

    runner = backtest_runner
    if runner is None:
        if market_data_service is None:
            raise ValueError(
                "market_data_service is required when backtest_runner is not provided"
            )
        backtest = config.backtest
        runner = DefaultBacktestRunner.from_market_data(
            market_data_service,
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=backtest.start,
            end=backtest.end,
        )

    with _lock:
        _jobs[study_id] = job
    _persist_progress(job)
    _executor.submit(_run_job, job, runner)
    return job


def request_cancel(study_id: str) -> Optional[OptimizationJob]:
    job = get_job(study_id)
    if job is None:
        return None
    if job.status in ("pending", "running"):
        job.cancel_requested = True
        job.updated_at = _now()
        _persist_progress(job)
    return job


def _make_progress_cb(
    job: OptimizationJob,
) -> Callable[[optuna.Study, optuna.trial.FrozenTrial], None]:
    is_multi = job.config.is_multi_objective()

    def _cb(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        job.completed_trials = sum(
            1 for t in study.trials if t.state.is_finished()
        )
        if not is_multi:
            try:
                job.best_value = study.best_value
                job.best_params = dict(study.best_params)
            except ValueError:
                pass
        job.updated_at = _now()
        _persist_progress(job)
        if trial.state.is_finished():
            _persist_trial(job.db_study_id, trial)
        if job.cancel_requested:
            study.stop()

    return _cb


def _best_value(result: OptimizationResult) -> Optional[float]:
    if result.best_trial is None or result.best_trial.values is None:
        return None
    values = result.best_trial.values
    return values[0] if len(values) == 1 else None


def _run_job(
    job: OptimizationJob,
    backtest_runner: Optional[BacktestRunner] = None,
) -> None:
    job.status = "running"
    job.updated_at = _now()
    _persist_study_status(job.db_study_id, status="running")
    _persist_progress(job)
    try:
        runner = OptimizationRunner(
            job.config,
            backtest_runner or DefaultBacktestRunner(),
        )
        result = runner.run(callbacks=[_make_progress_cb(job)])
        job.result = result
        job.best_params = result.best_params
        job.best_value = _best_value(result)
        job.status = "cancelled" if job.cancel_requested else "done"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Optimization job %s failed", job.study_id)
        job.status = "error"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        _persist_study_finish(job)
        _persist_progress(job)


def status_payload(job: OptimizationJob) -> dict[str, Any]:
    return {
        "study_id": job.study_id,
        "status": job.status,
        "completed_trials": job.completed_trials,
        "n_trials": job.n_trials,
        "best_value": job.best_value,
        "best_params": job.best_params,
        "error": job.error,
    }


def results_payload(job: OptimizationJob) -> Optional[dict[str, Any]]:
    result = job.result
    if result is None:
        return None
    return {
        "study_id": job.study_id,
        "objective_mode": job.config.objective.mode.value,
        "is_multi_objective": job.config.is_multi_objective(),
        "best_params": result.best_params,
        "best_trial": (
            serialize_trial(result.best_trial)
            if result.best_trial is not None
            else None
        ),
        "trials": [serialize_trial(t) for t in result.study.trials],
        "pareto_trials": [serialize_trial(t) for t in result.pareto_trials],
        "failures": result.failures,
    }


def get_persisted_study_status(study_id: str) -> Optional[str]:
    try:
        study_uuid = _parse_study_uuid(study_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            study = get_optimization_study(session, study_uuid)
    except Exception as exc:
        logger.warning("Failed to load optimization study %s from DB: %s", study_id, exc)
        return None
    return study.status if study is not None else None


def results_payload_from_db(study_id: str) -> Optional[dict[str, Any]]:
    try:
        study_uuid = _parse_study_uuid(study_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            study = get_optimization_study(session, study_uuid)
    except Exception as exc:
        logger.warning("Failed to load optimization study %s from DB: %s", study_id, exc)
        return None

    if study is None:
        return None

    if study.status not in ("done", "cancelled", "error"):
        return None

    config = study.config or {}
    snapshot = config.get("persisted_snapshot", {})
    config_for_validation = {
        key: value for key, value in config.items() if key != "persisted_snapshot"
    }
    try:
        opt_config = OptimizationConfig.model_validate(config_for_validation)
    except Exception:
        logger.warning("Invalid optimization config stored for study %s", study_id)
        return None

    serialized_trials = [
        _serialize_db_trial(trial.metrics, trial)
        for trial in sorted(study.trials, key=lambda item: item.trial_number)
    ]
    trials_by_number = {trial["number"]: trial for trial in serialized_trials}

    best_trial_number = snapshot.get("best_trial_number")
    best_trial = (
        trials_by_number.get(best_trial_number)
        if best_trial_number is not None
        else None
    )
    pareto_numbers = snapshot.get("pareto_trial_numbers", [])
    pareto_trials = [
        trials_by_number[number]
        for number in pareto_numbers
        if number in trials_by_number
    ]

    return {
        "study_id": study_id,
        "objective_mode": opt_config.objective.mode.value,
        "is_multi_objective": opt_config.is_multi_objective(),
        "best_params": snapshot.get("best_params", {}),
        "best_trial": best_trial,
        "trials": serialized_trials,
        "pareto_trials": pareto_trials,
        "failures": snapshot.get("failures", []),
    }


def study_list_item_from_db(study) -> dict[str, Any]:
    config = study.config or {}
    snapshot = config.get("persisted_snapshot", {})
    n_trials = config.get("study", {}).get("n_trials", 0)
    return {
        "study_id": study.id.hex,
        "name": study.name,
        "status": study.status,
        "best_value": snapshot.get("best_value"),
        "n_trials": n_trials,
        "completed_trials": _count_completed_trials(study.trials),
        "created_at": study.created_at,
    }
