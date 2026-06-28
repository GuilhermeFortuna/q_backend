"""Dramatiq actors — the leaf units of CPU work that drain into the worker pool.

Job managers in ``q_backend.api.*`` enqueue these instead of running work in
in-process thread pools. The actors stay thin: each one delegates to an
orchestration function in the relevant job manager, where the persistence helpers
already live. Job-manager functions are imported lazily inside each actor to avoid
an import cycle (the managers import ``q_backend.tasks.fanin``/``.cpu``, which would
otherwise re-enter this module during package import).

NOTE: no actor may spawn its own ProcessPoolExecutor — the worker pool is the only
source of parallelism. Leaf work runs sequentially within the actor process.
"""

import logging

import dramatiq

from q_backend.tasks.broker import DEFAULT_MAX_RETRIES, DEFAULT_TIME_LIMIT_MS

logger = logging.getLogger(__name__)

_ACTOR_OPTS = dict(max_retries=DEFAULT_MAX_RETRIES, time_limit=DEFAULT_TIME_LIMIT_MS)


@dramatiq.actor(max_retries=DEFAULT_MAX_RETRIES, time_limit=60_000)
def ping(note: str = "") -> None:
    """Connectivity smoke-test actor: confirms a message round-trips to a worker."""
    logger.info("ping actor received: %s", note)


# --- optimization -----------------------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def optimization_coordinator(
    study_id: str, db_study_id_hex: str, config_json: str
) -> None:
    from q_backend.api import optimization_jobs

    optimization_jobs.dispatch_study(study_id, db_study_id_hex, config_json)


@dramatiq.actor(**_ACTOR_OPTS)
def run_optimization_trials(
    study_id: str, db_study_id_hex: str, config_json: str, n_trials: int
) -> None:
    from q_backend.api import optimization_jobs

    optimization_jobs.run_trials_chunk(study_id, db_study_id_hex, config_json, n_trials)


# --- walk-forward -----------------------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def walkforward_coordinator(run_id: str, db_run_id_hex: str, request_json: str) -> None:
    from q_backend.api import walkforward_jobs

    walkforward_jobs.dispatch_windows(run_id, db_run_id_hex, request_json)


@dramatiq.actor(**_ACTOR_OPTS)
def run_walkforward_window(
    run_id: str,
    db_run_id_hex: str,
    request_json: str,
    window_index: int,
    total_windows: int,
) -> None:
    from q_backend.api import walkforward_jobs

    walkforward_jobs.run_window(
        run_id, db_run_id_hex, request_json, window_index, total_windows
    )


# --- discovery (strategy search) --------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def discovery_coordinator(run_id: str, db_run_id_hex: str, config_json: str) -> None:
    from q_backend.api import strategy_search_jobs

    strategy_search_jobs.dispatch_candidates(run_id, db_run_id_hex, config_json)


@dramatiq.actor(**_ACTOR_OPTS)
def evaluate_discovery_candidate(
    run_id: str, db_run_id_hex: str, config_json: str, candidate_index: int
) -> None:
    from q_backend.api import strategy_search_jobs

    strategy_search_jobs.run_candidate(
        run_id, db_run_id_hex, config_json, candidate_index
    )


@dramatiq.actor(**_ACTOR_OPTS)
def evaluate_genetic_candidate(
    run_id: str,
    db_run_id_hex: str,
    config_json: str,
    generation: int,
    candidate_index: int,
) -> None:
    from q_backend.api import strategy_search_jobs

    strategy_search_jobs.run_genetic_candidate(
        run_id, db_run_id_hex, config_json, generation, candidate_index
    )


# --- backtest ---------------------------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def run_backtest(run_id: str, request_json: str) -> None:
    from q_backend.api import backtest_jobs

    backtest_jobs.run_backtest_job(run_id, request_json)


# --- neural training (WO147) ----------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def run_neural_training(job_id: str, request_json: str) -> None:
    from q_backend.api import neural_jobs

    neural_jobs.run_training_job(job_id, request_json)


# --- storage ingest (WO48) ------------------------------------------------------


@dramatiq.actor(**_ACTOR_OPTS)
def run_storage_ingest(job_id: str, request_json: str) -> None:
    from q_backend.api import storage_jobs

    storage_jobs.run_ingest_job(job_id, request_json)
