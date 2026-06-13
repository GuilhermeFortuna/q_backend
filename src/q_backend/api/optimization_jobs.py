"""Job manager for asynchronous Optuna optimization runs.

Studies are dispatched to the Dramatiq worker pool. A coordinator splits the trial
budget across several trial-worker messages that collaborate on one distributed
Optuna study (shared Postgres storage); the last to finish runs the finalizer.
Progress is read from the database and Redis — no per-study state lives in the API
process.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

import optuna
import pandas as pd

from q_backend.optimization import (
    BacktestRunner,
    DefaultBacktestRunner,
    OptimizationConfig,
    OptimizationResult,
    OptimizationRunner,
    TickBacktestRunner,
    serialize_trial,
)
from q_backend.optimization.models import StorageConfig
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.storage import load_or_create_study
from q_backend.optimization.tick_backtest_runner import resolve_tick_flags
from q_backend.tasks.cpu import fan_out_count
from q_backend.tasks.data import load_ohlcv_frame, prime_ohlcv_cache
from q_backend.tasks.fanin import (
    clear_job_keys,
    decrement_and_is_last,
    init_counter,
    is_cancelled,
    set_cancelled,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import OptimizationStudy, RunStatus, TrialStatus
from q_backend.storage.db.repositories import (
    create_optimization_study,
    create_optimization_trial,
    get_optimization_study,
    get_optimization_trial_by_number,
    mark_active_runs_cancelled,
    update_optimization_study,
    update_optimization_trial,
)
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import (
    delete_job_progress,
    get_job_progress,
    set_job_progress,
)

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
        "workers",
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
    workers: int = 1
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


def _compute_study_workers(
    config: OptimizationConfig,
    *,
    backtest_runner: Optional[BacktestRunner] = None,
) -> int:
    if backtest_runner is not None or config.backtest.engine == "tick":
        return 1
    return resolve_worker_count(config.study.max_workers, config.study.n_trials)


def _trial_fan_out_count(config: OptimizationConfig) -> int:
    """How many Dramatiq trial-chunk messages to dispatch for this study."""
    if _compute_study_workers(config) > 1:
        # Candle parallel path owns CPU via an internal process pool.
        return 1
    return fan_out_count(config.study.n_trials)


def get_job(study_id: str) -> Optional[OptimizationJob]:
    """Studies no longer live in the API process; status is read from DB/Redis."""
    return None


def evict_study(study_id: str) -> None:
    """Remove cached Redis progress and fan-in bookkeeping for a study."""
    try:
        delete_job_progress(get_redis(), study_id)
    except Exception:
        logger.debug("Redis progress delete unavailable for study %s", study_id)
    clear_job_keys(study_id)


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
    return _OPTUNA_STATE_TO_TRIAL_STATUS.get(trial.state, TrialStatus.PENDING.value)


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


def _persist_study_finish(job: OptimizationJob, terminal_status: JobStatus) -> None:
    if job.db_study_id is None:
        return
    result = job.result
    snapshot: dict[str, Any] = {
        "best_params": job.best_params,
        "best_value": job.best_value,
        "failures": result.failures if result is not None else [],
        "error": job.error,
        "workers": job.workers,
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
                status=terminal_status,
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


def _optimization_config_from_study_config(
    config: dict[str, Any],
) -> dict[str, Any] | None:
    config_for_validation = {
        key: value for key, value in config.items() if key != "persisted_snapshot"
    }
    try:
        return OptimizationConfig.model_validate(config_for_validation).model_dump(
            mode="json"
        )
    except Exception:
        logger.debug("Invalid optimization config stored for study")
        return None


def status_payload_from_db(study_id: str) -> dict[str, Any] | None:
    try:
        study_uuid = _parse_study_uuid(study_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            study = get_optimization_study(session, study_uuid)
    except Exception as exc:
        logger.warning(
            "Failed to load optimization study %s from DB: %s", study_id, exc
        )
        return None

    if study is None:
        return None

    config = study.config or {}
    snapshot = config.get("persisted_snapshot", {})
    n_trials = config.get("study", {}).get("n_trials", 0)
    optimization_config = _optimization_config_from_study_config(config)
    backtest_config = (
        optimization_config.get("backtest") if optimization_config is not None else None
    )

    return {
        "study_id": study_id,
        "status": study.status,
        "completed_trials": _count_completed_trials(study.trials),
        "n_trials": n_trials,
        "best_value": snapshot.get("best_value"),
        "best_params": snapshot.get("best_params", {}),
        "error": snapshot.get("error"),
        "workers": snapshot.get("workers", 1),
        "backtest_config": backtest_config,
        "optimization_config": optimization_config,
    }


def _enrich_status_with_config(
    payload: dict[str, Any], study_id: str
) -> dict[str, Any]:
    if payload.get("optimization_config") is not None:
        return payload
    db_payload = status_payload_from_db(study_id)
    if db_payload is None:
        return payload
    enriched = dict(payload)
    if db_payload.get("optimization_config") is not None:
        enriched["optimization_config"] = db_payload["optimization_config"]
    if db_payload.get("backtest_config") is not None:
        enriched["backtest_config"] = db_payload["backtest_config"]
    return enriched


def _apply_cancel_overlay(
    study_id: str, payload: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Reflect a raised cancel flag immediately, before DB/Redis mirrors catch up."""
    if payload is None:
        return None
    if is_cancelled(study_id) and payload.get("status") in ("pending", "running"):
        return {**payload, "status": "cancelled"}
    return payload


def get_status_payload(study_id: str) -> dict[str, Any] | None:
    job = get_job(study_id)
    if job is not None:
        payload = status_payload(job)
        optimization_config = job.config.model_dump(mode="json")
        payload["backtest_config"] = optimization_config.get("backtest")
        payload["optimization_config"] = optimization_config
        return _apply_cancel_overlay(study_id, payload)

    db_payload = status_payload_from_db(study_id)

    try:
        cached = get_job_progress(get_redis(), study_id)
        if cached is not None and db_payload is None:
            return _apply_cancel_overlay(
                study_id, _enrich_status_with_config(cached, study_id)
            )
    except Exception:
        logger.debug("Redis progress read unavailable for study %s", study_id)

    return _apply_cancel_overlay(study_id, db_payload)


def start_job(
    config: OptimizationConfig,
    backtest_runner: Optional[BacktestRunner] = None,
    market_data_service: Any | None = None,
) -> OptimizationJob:
    """Persist the study and dispatch it to the worker pool."""
    workers = _compute_study_workers(config, backtest_runner=backtest_runner)

    if (
        backtest_runner is None
        and config.backtest.engine == "candle"
        and market_data_service is not None
    ):
        backtest = config.backtest
        prime_ohlcv_cache(
            market_data_service,
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=backtest.start,
            end=backtest.end,
        )

    study_id, db_study_id = _persist_study_start(config)
    job = OptimizationJob(
        study_id=study_id,
        db_study_id=db_study_id,
        config=config,
        n_trials=config.study.n_trials,
        workers=workers,
    )
    _persist_progress(job)

    from q_backend.tasks import actors

    actors.optimization_coordinator.send(
        study_id,
        db_study_id.hex if db_study_id is not None else "",
        config.model_dump_json(),
    )
    return job


def _mirror_cancelled_in_redis(study_id: str) -> None:
    """Mark the Redis progress mirror cancelled when the DB row is unavailable."""
    try:
        cached = get_job_progress(get_redis(), study_id)
        if cached is None:
            return
        mirrored = dict(cached)
        mirrored["status"] = "cancelled"
        set_job_progress(get_redis(), study_id, mirrored)
    except Exception:
        logger.debug("Redis progress update unavailable for study %s", study_id)


def request_cancel(study_id: str) -> Optional[OptimizationJob]:
    # Raise the cancel flag so any in-flight trial workers stop and pending trial
    # messages drain as no-ops. Also flip the DB/Redis state immediately so the UI
    # clears right away (and so a study with no live workers — an orphan from a
    # previous process — is resolved too); the finalizer re-persists consistently.
    set_cancelled(study_id)
    cancelled_in_db = _cancel_orphaned_study(study_id)
    if not cancelled_in_db:
        _mirror_cancelled_in_redis(study_id)
    return None


def _cancel_orphaned_study(study_id: str) -> bool:
    try:
        study_uuid = _parse_study_uuid(study_id)
    except ValueError:
        return False
    try:
        with session_scope() as session:
            study = get_optimization_study(session, study_uuid)
            if study is None or study.status not in ("pending", "running"):
                return False
            update_optimization_study(session, study_uuid, status="cancelled")
    except Exception as exc:
        logger.warning(
            "Failed to cancel orphaned optimization study %s: %s", study_id, exc
        )
        return False
    try:
        delete_job_progress(get_redis(), study_id)
    except Exception:
        logger.debug("Redis progress delete unavailable for study %s", study_id)
    return True


def reconcile_orphaned_runs() -> int:
    """Cancel studies left active by a previous process. Call once on startup.

    The in-memory job registry is empty at startup, so any study still marked
    pending/running in the DB has no live worker and will never finish.
    """
    try:
        with session_scope() as session:
            count = mark_active_runs_cancelled(session, OptimizationStudy)
    except Exception as exc:
        logger.warning("Failed to reconcile orphaned optimization studies: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned optimization study(ies) on startup.", count)
    return count


def _make_progress_cb(
    job: OptimizationJob,
) -> Callable[[optuna.Study, optuna.trial.FrozenTrial], None]:
    is_multi = job.config.is_multi_objective()

    def _cb(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        job.completed_trials = sum(1 for t in study.trials if t.state.is_finished())
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
        if job.cancel_requested or is_cancelled(job.study_id):
            job.cancel_requested = True

    return _cb


def _best_value(result: OptimizationResult) -> Optional[float]:
    if result.best_trial is None or result.best_trial.values is None:
        return None
    values = result.best_trial.values
    return values[0] if len(values) == 1 else None


def _build_worker_runner(
    config: OptimizationConfig,
) -> tuple[BacktestRunner, pd.DataFrame | None]:
    """Build a backtest runner inside a worker process from cached market data."""
    from q_backend.tasks.worker_context import get_worker_market_data_service

    backtest = config.backtest
    if backtest.engine == "tick":
        return (
            TickBacktestRunner.from_market_data(
                get_worker_market_data_service(),
                symbol=backtest.symbol,
                start=backtest.start,
                end=backtest.end,
                flags=resolve_tick_flags(backtest.tick_flags),
            ),
            None,
        )
    frame = load_ohlcv_frame(
        backtest.symbol, backtest.timeframe, backtest.start, backtest.end
    )
    return DefaultBacktestRunner.from_frame_sliced(frame), frame


def _distributed_config(
    config: OptimizationConfig, study_id: str
) -> OptimizationConfig:
    """Point a config at the shared Postgres study unique to this run."""
    worker_config = config.model_copy(deep=True)
    worker_config.study.storage = StorageConfig(type="shared")
    worker_config.study.name = f"opt-{study_id}"
    return worker_config


def _split_evenly(total: int, parts: int) -> list[int]:
    base, remainder = divmod(total, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def dispatch_study(study_id: str, db_study_id_hex: str, config_json: str) -> None:
    """Coordinator: create the shared study and fan trials out to worker messages."""
    config = OptimizationConfig.model_validate_json(config_json)
    db_study_id = uuid.UUID(db_study_id_hex) if db_study_id_hex else None

    if is_cancelled(study_id):
        finalize_study(study_id, db_study_id_hex, config_json)
        return

    workers = _compute_study_workers(config)
    _persist_study_status(db_study_id, status="running")
    running = OptimizationJob(
        study_id=study_id,
        db_study_id=db_study_id,
        config=config,
        n_trials=config.study.n_trials,
        status="running",
        workers=workers,
    )
    _persist_progress(running)

    # Create the distributed study once so trial workers only ever load it.
    load_or_create_study(_distributed_config(config, study_id))

    leaf_messages = _trial_fan_out_count(config)
    init_counter(study_id, leaf_messages)

    from q_backend.tasks import actors

    for chunk in _split_evenly(config.study.n_trials, leaf_messages):
        actors.run_optimization_trials.send(
            study_id, db_study_id_hex, config_json, chunk
        )


def run_trials_chunk(
    study_id: str, db_study_id_hex: str, config_json: str, n_trials: int
) -> None:
    """Trial worker: optimize a chunk of trials against the shared study."""
    config = OptimizationConfig.model_validate_json(config_json)
    db_study_id = uuid.UUID(db_study_id_hex) if db_study_id_hex else None

    if not is_cancelled(study_id):
        workers = _compute_study_workers(config)
        job = OptimizationJob(
            study_id=study_id,
            db_study_id=db_study_id,
            config=config,
            n_trials=config.study.n_trials,
            status="running",
            workers=workers,
        )
        try:
            runner, ohlcv = _build_worker_runner(config)
            runner_kwargs: dict[str, Any] = {
                "ohlcv": ohlcv,
                "max_workers": config.study.max_workers,
            }
            opt_runner = OptimizationRunner(
                _distributed_config(config, study_id),
                runner,
                **runner_kwargs,
            )
            opt_runner.run(
                callbacks=[_make_progress_cb(job)],
                n_trials=n_trials,
                should_stop=lambda: is_cancelled(study_id),
            )
        except Exception:  # noqa: BLE001 - one chunk failing shouldn't strand the study
            logger.exception("Optimization trial chunk failed for study %s", study_id)

    if decrement_and_is_last(study_id):
        finalize_study(study_id, db_study_id_hex, config_json)


def finalize_study(study_id: str, db_study_id_hex: str, config_json: str) -> None:
    """Last worker: read the completed study, persist results, mark terminal."""
    config = OptimizationConfig.model_validate_json(config_json)
    db_study_id = uuid.UUID(db_study_id_hex) if db_study_id_hex else None
    job = OptimizationJob(
        study_id=study_id,
        db_study_id=db_study_id,
        config=config,
        n_trials=config.study.n_trials,
    )
    terminal_status: JobStatus = "error"
    try:
        # n_trials=0 runs no new trials; it just loads the shared study and
        # computes the best/pareto result from the trials the workers produced.
        result = OptimizationRunner(
            _distributed_config(config, study_id), DefaultBacktestRunner()
        ).run(n_trials=0)
        job.result = result
        job.completed_trials = sum(
            1 for trial in result.study.trials if trial.state.is_finished()
        )
        job.best_params = result.best_params
        job.best_value = _best_value(result)
        job.workers = _compute_study_workers(config)
        terminal_status = "cancelled" if is_cancelled(study_id) else "done"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Optimization finalize failed for study %s", study_id)
        terminal_status = "error"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        # Persist to the DB *before* flipping the status to terminal, so a watcher
        # that observes the terminal status never reads a stale "running" DB row.
        _persist_study_finish(job, terminal_status)
        job.status = terminal_status
        _persist_progress(job)
        clear_job_keys(study_id)


def status_payload(job: OptimizationJob) -> dict[str, Any]:
    return {
        "study_id": job.study_id,
        "status": job.status,
        "completed_trials": job.completed_trials,
        "n_trials": job.n_trials,
        "best_value": job.best_value,
        "best_params": job.best_params,
        "error": job.error,
        "workers": job.workers,
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
        logger.warning(
            "Failed to load optimization study %s from DB: %s", study_id, exc
        )
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
        logger.warning(
            "Failed to load optimization study %s from DB: %s", study_id, exc
        )
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
