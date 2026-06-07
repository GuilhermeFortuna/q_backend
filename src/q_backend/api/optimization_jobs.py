"""In-memory job manager for asynchronous Optuna optimization runs.

The optimization runner (`q_backend.optimization`) blocks for the full duration
of a study (default 50 trials, each a full backtest). To expose it over HTTP we
run each study on a background worker thread and report progress via polling.

State is in-memory only: this is a single-user, local-desktop deployment, so a
process-lifetime registry is sufficient (no DB/sqlite needed for v1).
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

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "done", "error", "cancelled"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OptimizationJob:
    study_id: str
    config: OptimizationConfig
    n_trials: int
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
# Dedicated pool so long-running studies never starve the request threadpool.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="optimize")


def get_job(study_id: str) -> Optional[OptimizationJob]:
    with _lock:
        return _jobs.get(study_id)


def start_job(
    config: OptimizationConfig,
    backtest_runner: Optional[BacktestRunner] = None,
    market_data_service: Any | None = None,
) -> OptimizationJob:
    study_id = uuid.uuid4().hex
    job = OptimizationJob(
        study_id=study_id,
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
    _executor.submit(_run_job, job, runner)
    return job


def request_cancel(study_id: str) -> Optional[OptimizationJob]:
    job = get_job(study_id)
    if job is None:
        return None
    if job.status in ("pending", "running"):
        job.cancel_requested = True
        job.updated_at = _now()
    return job


def _make_progress_cb(
    job: OptimizationJob,
) -> Callable[[optuna.Study, optuna.trial.FrozenTrial], None]:
    is_multi = job.config.is_multi_objective()

    def _cb(study: optuna.Study, _trial: optuna.trial.FrozenTrial) -> None:
        job.completed_trials = sum(
            1 for t in study.trials if t.state.is_finished()
        )
        if not is_multi:
            try:
                job.best_value = study.best_value
                job.best_params = dict(study.best_params)
            except ValueError:
                # No completed trial yet.
                pass
        job.updated_at = _now()
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
