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

from q_backend.backtesting.genome.activity import genome_signal_activity
from q_backend.backtesting.genome.exit_rule_policy import exit_policy_metadata_for_genome
from q_backend.backtesting.genome.operators import genome_node_count
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.models import Trade
from q_backend.optimization.exit_quality import summarize_exit_quality
from q_backend.optimization.backtest_runner import BacktestRunner, DefaultBacktestRunner
from q_backend.optimization.genetic_search import (
    GeneticCandidateProvider,
    GeneticStrategySearchOrchestrator,
    _complexity_penalty,
    _config_for_walkforward,
    create_genetic_candidate_provider,
    search_candidate_for_genome,
)
from q_backend.optimization.lockbox import compute_lockbox_bounds
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    GeneticSearchConfig,
    RegistryCandidateProvider,
    SearchProgress,
    StrategySearchConfig,
    StrategySearchResult,
    _rank_results,
    _unsupported_result,
    evaluate_candidate,
)
from q_backend.optimization.walkforward import split_windows
from q_backend.market_data.exogenous_context import get_cached_exogenous_provenance
from q_backend.market_data.exogenous_config import validate_exogenous_for_primary
from q_backend.tasks.data import load_evaluation_frame, load_ohlcv_frame
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
from q_backend.tasks import genetic_staging
from q_backend.tasks.staging import clear_partials, load_partials, stash_partial
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import RunStatus, StrategySearchRun
from q_backend.storage.db.repositories import (
    create_strategy_search_candidate,
    create_strategy_search_run,
    get_strategy_search_run,
    get_strategy_search_candidate,
    mark_active_runs_cancelled,
    update_strategy_search_run,
)
from q_backend.storage.lake.artifacts import (
    delete_strategy_search_artifacts,
    read_strategy_search_candidate_artifact,
    read_strategy_search_candidate_genome,
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
    # Float so genetic discovery can report fractional progress (completed walk-forward
    # windows / windows-per-candidate); the registry path still sets whole candidates.
    current_candidate: float = 0
    total_candidates: int = 0
    candidate_id: Optional[str] = None
    strategy: Optional[str] = None
    phase: Optional[Literal["optimizing", "testing", "done"]] = None
    window_index: Optional[int] = None
    total_windows: Optional[int] = None
    generation: Optional[int] = None
    total_generations: Optional[int] = None
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
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.debug(
            "Redis progress delete unavailable for strategy search run %s", run_id
        )
    clear_partials(run_id)
    clear_job_keys(run_id)


def validate_strategy_search_request(config: StrategySearchConfig) -> None:
    validate_exogenous_for_primary(
        primary_symbol=config.backtest.symbol,
        primary_timeframe=config.backtest.timeframe,
        exogenous_series=config.exogenous_series,
    )
    wf_end, _, _ = compute_lockbox_bounds(
        config.backtest.start, config.backtest.end, config.lockbox
    )
    split_windows(config.backtest.start, wf_end, config.walkforward)


def _load_run_frame(request: StrategySearchConfig) -> pd.DataFrame:
    if request.exogenous_series:
        return load_evaluation_frame(request)
    backtest = request.backtest
    return load_ohlcv_frame(
        backtest.symbol, backtest.timeframe, backtest.start, backtest.end
    )


def _serialize_search_config(config: StrategySearchConfig) -> dict[str, Any]:
    """Serialize config, omitting default genetic/lockbox blocks for registry compat."""
    payload = config.model_dump(mode="json", exclude_none=True)
    if config.genetic is None:
        payload.pop("genetic", None)
    if not config.lockbox.enabled:
        payload.pop("lockbox", None)
    if config.genetic is not None:
        payload["provider"] = "genetic"
    if config.exogenous_series:
        provenance = config.exogenous_provenance
        if not provenance:
            provenance = get_cached_exogenous_provenance(
                primary_symbol=config.backtest.symbol,
                primary_timeframe=config.backtest.timeframe,
                start=config.backtest.start,
                end=config.backtest.end,
                exogenous_series=config.exogenous_series,
            )
        if provenance:
            payload["exogenous_provenance"] = provenance
    return payload


def _request_config_payload(config: StrategySearchConfig) -> dict[str, Any]:
    return _serialize_search_config(config)


def _persist_progress(job: StrategySearchJob) -> None:
    try:
        set_job_progress(
            get_redis(),
            job.run_id,
            status_payload(job),
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning("Failed to persist strategy search run status: %s", exc)


def _failure_reason_counts(candidates: list[CandidateResult]) -> dict[str, int]:
    """Tally why candidates died so a discovery run reports it, never hides it.

    A candidate that fails to produce a completed walk-forward result carries a
    non-``completed`` status and an ``error`` string; without this tally those
    deaths are only visible by scanning the per-candidate list. Grouping by
    reason surfaces "N candidates died and why" directly in the result summary.
    """
    reasons: dict[str, int] = {}
    for candidate in candidates:
        if candidate.status == "completed":
            continue
        reason = candidate.error or candidate.status
        reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def _build_result_summary(result: StrategySearchResult) -> dict[str, Any]:
    best = result.best
    summary: dict[str, Any] = {
        "objective_mode": result.objective_mode.value,
        "candidate_count": len(result.candidates),
        "ranked_count": sum(
            1 for candidate in result.candidates if candidate.rank is not None
        ),
        "passed_gates_count": sum(
            1 for candidate in result.candidates if candidate.passed_gates
        ),
        "failed_candidate_count": sum(
            1 for candidate in result.candidates if candidate.status != "completed"
        ),
        "failure_reasons": _failure_reason_counts(result.candidates),
        "best_candidate_id": best.candidate_id if best is not None else None,
        "best_strategy": best.strategy if best is not None else None,
        "best_objective_value": best.objective_value if best is not None else None,
        "best_efficiency": best.efficiency if best is not None else None,
    }
    genetic_summary = result.genetic_summary
    if genetic_summary is not None:
        summary.update(
            {
                "generations_completed": genetic_summary.generations_completed,
                "total_genomes_evaluated": genetic_summary.total_genomes_evaluated,
                "champion_dsr": genetic_summary.champion_dsr,
                "n_trials_effective": genetic_summary.n_trials_effective,
                "sr_observed": genetic_summary.sr_observed,
                "lockbox_metrics": genetic_summary.lockbox_metrics,
                "lockbox_passed": genetic_summary.lockbox_passed,
            }
        )
    return summary


def _build_candidate_trades_dataframes(
    candidates: list[CandidateResult],
) -> dict[str, pd.DataFrame]:
    trades: dict[str, pd.DataFrame] = {}
    for candidate in candidates:
        if not candidate.oos_trades:
            continue
        trades[candidate.candidate_id] = pd.DataFrame(
            [trade.model_dump(mode="json") for trade in candidate.oos_trades]
        )
    return trades


def _exit_quality_from_diagnostics(diagnostics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not diagnostics:
        return None
    exit_quality = diagnostics.get("exit_quality")
    return exit_quality if isinstance(exit_quality, dict) else None


def _attach_exit_quality_fields(
    payload: dict[str, Any],
    *,
    diagnostics: dict[str, Any] | None,
    run_id: str | None = None,
    candidate_id: str | None = None,
    bars: pd.DataFrame | None = None,
) -> None:
    exit_quality = _exit_quality_from_diagnostics(diagnostics)
    if exit_quality is None and run_id is not None and candidate_id is not None:
        exit_quality = _rebuild_exit_quality_from_lake(
            run_id, candidate_id, bars=bars
        )
    if exit_quality is not None:
        payload["exit_quality"] = exit_quality
    if diagnostics:
        payload["diagnostics"] = diagnostics


def _rebuild_exit_quality_from_lake(
    run_id: str,
    candidate_id: str,
    *,
    bars: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    try:
        trades_df = read_strategy_search_candidate_artifact(
            run_id, candidate_id, "oos_trades"
        )
    except FileNotFoundError:
        return None
    if trades_df.empty:
        return None
    trades = [Trade.model_validate(record) for record in trades_df.to_dict("records")]
    return summarize_exit_quality(trades, bars=bars)


def _build_leaderboard_dataframe(
    candidates: list[CandidateResult],
    *,
    candidate_metadata: dict[str, dict[str, Any]] | None = None,
    generation: int | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    metadata = candidate_metadata or {}
    for candidate in candidates:
        meta = metadata.get(candidate.candidate_id, {})
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
                "generation": meta.get("generation", generation),
                "genome_node_count": meta.get("genome_node_count"),
                "dsr": meta.get("dsr"),
                "complexity_penalty": meta.get("complexity_penalty"),
                "exit_preset_id": meta.get("exit_preset_id"),
                "exit_preset_label": meta.get("exit_preset_label"),
                "exit_param_names_json": json.dumps(meta.get("exit_param_names") or []),
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
        metadata = result.candidate_metadata or {}
        generation_leaderboards: dict[int, pd.DataFrame] | None = None
        if result.all_generations is not None:
            generation_leaderboards = {
                generation_index: _build_leaderboard_dataframe(
                    generation_results,
                    candidate_metadata=metadata,
                    generation=generation_index,
                )
                for generation_index, generation_results in enumerate(
                    result.all_generations
                )
            }
        candidate_genomes = {
            candidate_id: meta["genome"]
            for candidate_id, meta in metadata.items()
            if meta.get("genome") is not None
        }
        lockbox_equity = None
        genetic_summary = result.genetic_summary
        if (
            genetic_summary is not None
            and genetic_summary.lockbox_equity_curve is not None
        ):
            series = genetic_summary.lockbox_equity_curve
            lockbox_equity = pd.DataFrame({"time": series.index, "equity": series.values})
        return write_strategy_search_artifacts(
            run_id,
            _build_leaderboard_dataframe(
                result.candidates, candidate_metadata=metadata
            ),
            _build_candidate_equity_dataframes(result.candidates),
            candidate_trades=_build_candidate_trades_dataframes(
                _candidates_for_persistence(result)
            ),
            generation_leaderboards=generation_leaderboards,
            candidate_genomes=candidate_genomes or None,
            lockbox_equity=lockbox_equity,
            lockbox_metrics=(
                genetic_summary.lockbox_metrics if genetic_summary is not None else None
            ),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning("Failed to write strategy search lake artifacts: %s", exc)
        return None


def _persist_run_finish(job: StrategySearchJob, terminal_status: JobStatus) -> None:
    if job.db_run_id is None:
        return
    result = job.result
    result_summary = _build_result_summary(result) if result is not None else None
    metadata = result.candidate_metadata if result is not None else None
    try:
        with session_scope() as session:
            if result is not None:
                candidates_to_persist = _candidates_for_persistence(result)
                for candidate in candidates_to_persist:
                    meta = (metadata or {}).get(candidate.candidate_id, {})
                    from q_backend.optimization.hypothesis import extract_hypothesis_metadata
                    hyp_info = extract_hypothesis_metadata(meta.get("genome"))
                    
                    profile_ver = meta.get("profile_version") or hyp_info.get("profile_version")
                    hyp_id = meta.get("hypothesis_id") or hyp_info.get("hypothesis_id")
                    hyp_rat = meta.get("hypothesis_rationale") or hyp_info.get("hypothesis_rationale")
                    hyp_req = meta.get("hypothesis_required_features") or hyp_info.get("hypothesis_required_features")
                    hyp_hash = meta.get("hypothesis_template_hash") or hyp_info.get("hypothesis_template_hash")

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
                        generation=meta.get("generation"),
                        genome=meta.get("genome"),
                        genome_node_count=meta.get("genome_node_count"),
                        dsr=meta.get("dsr"),
                        complexity_penalty=meta.get("complexity_penalty"),
                        exit_preset_id=meta.get("exit_preset_id"),
                        exit_preset_label=meta.get("exit_preset_label"),
                        exit_policy_id=meta.get("exit_policy_id"),
                        exit_policy_label=meta.get("exit_policy_label"),
                        last_exit_mutation_op=meta.get("last_exit_mutation_op"),
                        exit_param_names=meta.get("exit_param_names"),
                        diagnostics=candidate.diagnostics,
                        profile_version=profile_ver,
                        hypothesis_id=hyp_id,
                        hypothesis_rationale=hyp_rat,
                        hypothesis_required_features=hyp_req,
                        hypothesis_template_hash=hyp_hash,
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning("Failed to persist strategy search run finish: %s", exc)


def _candidates_for_persistence(result: StrategySearchResult) -> list[CandidateResult]:
    if result.all_generations is not None:
        persisted: list[CandidateResult] = []
        for generation_results in result.all_generations:
            persisted.extend(generation_results)
        return persisted
    return list(result.candidates)


def _resolve_total_candidates(
    config: StrategySearchConfig,
    provider: CandidateProvider,
) -> int:
    if config.genetic is not None:
        assert config.genetic is not None
        return config.genetic.population_size * config.genetic.generations
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
    from q_backend.optimization.hypothesis import resolve_candidate_provider
    provider = resolve_candidate_provider(request)
    job = StrategySearchJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        total_candidates=_resolve_total_candidates(request, provider),
        total_generations=(
            request.genetic.generations if request.genetic is not None else None
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning(
            "Failed to cancel orphaned strategy search run %s: %s", run_id, exc
        )
        return False
    try:
        delete_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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
    return DefaultBacktestRunner.from_frame_sliced(_load_run_frame(request))


def _candidate_ohlcv(request: StrategySearchConfig) -> pd.DataFrame:
    return _load_run_frame(request)


def _genetic_probe_frame(request: StrategySearchConfig) -> pd.DataFrame | None:
    """Load the run's OHLCV once for gen-0 viability seeding, mutation repair, and
    the pre-screen. Returns ``None`` when both viability knobs are disabled so the
    staged path stays a no-op when the operator turns the feature off."""
    genetic = request.genetic
    if genetic is None:
        return None
    if genetic.min_seed_signals <= 0 and genetic.prescreen_min_signals <= 0:
        return None
    backtest = request.backtest
    try:
        return _load_run_frame(request)
    except Exception:  # noqa: BLE001 - viability is best-effort; never block a run
        logger.warning(
            "Genetic probe frame load failed for %s; trade-viability disabled",
            backtest.symbol,
        )
        return None


def _genetic_staged_metadata(
    genome: Genome,
    *,
    generation: int,
    genetic: GeneticSearchConfig,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generation": generation,
        "genome": genome.model_dump(),
        "genome_node_count": genome_node_count(genome),
        "complexity_penalty": _complexity_penalty(genome, genetic),
    }
    policy_meta = exit_policy_metadata_for_genome(genome)
    if policy_meta:
        meta.update(policy_meta)
    return meta


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
    generation: Optional[int] = None,
    total_generations: Optional[int] = None,
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
        generation=generation,
        total_generations=total_generations,
    )


def dispatch_candidates(run_id: str, db_run_id_hex: str, config_json: str) -> None:
    """Coordinator: fan each candidate out to its own worker message."""
    request = StrategySearchConfig.model_validate_json(config_json)
    if request.genetic is not None:
        dispatch_genetic_discovery(run_id, db_run_id_hex, config_json)
        return
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    from q_backend.optimization.hypothesis import resolve_candidate_provider
    provider = resolve_candidate_provider(request)
    candidates = list(provider.candidates())
    unsupported = []
    if hasattr(provider, "unsupported_names"):
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
    from q_backend.optimization.hypothesis import resolve_candidate_provider
    provider = resolve_candidate_provider(request)
    candidates = list(provider.candidates())
    candidate = candidates[candidate_index]

    if not is_cancelled(run_id):
        try:
            result = evaluate_candidate(
                candidate,
                request,
                _candidate_runner(request),
                ohlcv=_candidate_ohlcv(request),
                run_id=run_id,
            )
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


# --- genetic discovery: generation-barrier fan-out -------------------------------
#
# A genetic algorithm cannot fan all candidates out at once: generation N+1 is bred
# from generation N's fitness, so the population evolves between barriers. We fan out
# one generation's population across the worker pool, barrier on the shared fan-in
# counter, breed the next generation in the finalizer, and dispatch again — for G
# generations. The evolving provider state and the accumulated per-generation results
# live in Redis (``genetic_staging``) so any worker can resume the run.


def _walkforward_window_count(request: StrategySearchConfig) -> int:
    """Walk-forward windows each candidate evaluates (constant across candidates).

    Used to convert the global completed-window counter into a fractional candidate
    count so the progress bar moves on every window, not just on candidate completion.
    """
    wf_end, _, _ = compute_lockbox_bounds(
        request.backtest.start, request.backtest.end, request.lockbox
    )
    return len(split_windows(request.backtest.start, wf_end, request.walkforward))


def _persist_genetic_progress(
    run_id: str,
    db_run_id: Optional[uuid.UUID],
    request: StrategySearchConfig,
    *,
    generation: int,
    windows_per_candidate: int,
    candidate_id: Optional[str] = None,
) -> None:
    """Mirror live genetic progress, driven by completed windows for smoothness.

    ``current_candidate`` is fractional (completed_windows / windows_per_candidate);
    the bar advances every window. ``phase`` is kept non-null so the UI treats this as
    a live run (a null phase is the signal that only persisted/restart state remains).
    """
    genetic = request.genetic
    assert genetic is not None
    completed_windows = genetic_staging.get_completed_windows(run_id)
    current = completed_windows / windows_per_candidate if windows_per_candidate else 0
    _persist_progress(
        _progress_job(
            run_id,
            db_run_id,
            request,
            status="running",
            total_candidates=genetic.population_size * genetic.generations,
            current_candidate=current,
            candidate_id=candidate_id,
            strategy="CompositeStrategy",
            phase="optimizing",
            generation=generation + 1,
            total_generations=genetic.generations,
        )
    )


def dispatch_genetic_discovery(
    run_id: str, db_run_id_hex: str, config_json: str
) -> None:
    """Genetic coordinator: mark RUNNING, build generation 0, dispatch its population."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    assert request.genetic is not None

    if is_cancelled(run_id):
        _finalize_genetic(run_id, db_run_id_hex, config_json, cancelled=True)
        return

    _persist_run_status(
        db_run_id,
        status=RunStatus.RUNNING.value,
        clear_error_message=True,
        started_at=_now(),
    )

    provider = create_genetic_candidate_provider(
        request.genetic,
        request,
        probe_df=_genetic_probe_frame(request),
        latents_enabled=request.latents_enabled,
    )
    _dispatch_generation(run_id, db_run_id_hex, config_json, provider, generation=0)


def _dispatch_generation(
    run_id: str,
    db_run_id_hex: str,
    config_json: str,
    provider: GeneticCandidateProvider,
    generation: int,
) -> None:
    """Stash the generation's population and fan each genome out to a worker."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    genetic = request.genetic
    assert genetic is not None
    population = provider.population
    # The stashed state's population *is* this generation — candidate workers read
    # their genome from it by index, and the finalizer reloads it to breed the next.
    genetic_staging.set_provider_state(run_id, provider.export_state())
    # Each generation reuses the run-scoped fan-in counter and partial-staging hash,
    # cleared between generations so a generation's barrier counts only its own work.
    clear_partials(run_id)

    # Pre-screen: a genome with no in-sample signals can never score, so synthesize its
    # no_result here and skip the (expensive) walk-forward actor entirely. The probe
    # reuses the frame the provider already loaded for seeding/repair — no extra load.
    probe = getattr(provider, "_probe_df", None)
    alive_indices: list[int] = []
    for index, genome in enumerate(population):
        if (
            probe is not None
            and genetic.prescreen_min_signals > 0
            and not genome_signal_activity(
                genome,
                probe,
                min_signals=genetic.prescreen_min_signals,
                max_depth=genetic.max_depth,
                max_node_count=genetic.max_nodes,
            ).is_tradeable
        ):
            dead = CandidateResult(
                candidate_id=genome.genome_id,
                strategy="CompositeStrategy",
                status="no_result",
                error="pre-screen: no in-sample signals",
            )
            stash_partial(run_id, index, candidate_result_to_dict(dead))
            genetic_staging.set_candidate_meta(
                run_id,
                {
                    genome.genome_id: _genetic_staged_metadata(
                        genome,
                        generation=generation,
                        genetic=genetic,
                    )
                },
            )
        else:
            alive_indices.append(index)

    # The counter must be set before any actor is sent (an actor can finish and
    # decrement before this function returns), so it counts only the dispatched
    # (alive) candidates; pre-screened genomes already have their partials staged.
    init_counter(run_id, len(alive_indices))

    # Surface the generation bar immediately — before any candidate finishes — so the
    # UI shows "Generation N / G" and a non-zero baseline instead of sitting blank.
    _persist_genetic_progress(
        run_id,
        db_run_id,
        request,
        generation=generation,
        windows_per_candidate=_walkforward_window_count(request),
    )

    from q_backend.tasks import actors

    for index in alive_indices:
        actors.evaluate_genetic_candidate.send(
            run_id, db_run_id_hex, config_json, generation, index
        )

    # Whole generation pre-screened out: no actor will trip the fan-in barrier, so
    # breed/finalize now from the synthesized partials instead of hanging forever.
    if not alive_indices:
        finalize_generation(run_id, db_run_id_hex, config_json, generation)


def run_genetic_candidate(
    run_id: str,
    db_run_id_hex: str,
    config_json: str,
    generation: int,
    candidate_index: int,
) -> None:
    """Candidate worker: walk-forward evaluate one genome of one generation."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    genetic = request.genetic
    assert genetic is not None
    windows_per_candidate = _walkforward_window_count(request)

    candidate_id: Optional[str] = None
    if not is_cancelled(run_id):
        genome = Genome.model_validate(
            genetic_staging.get_generation_genome(run_id, candidate_index)
        )
        candidate_id = genome.genome_id
        candidate = search_candidate_for_genome(genome, request)

        def on_window(progress: SearchProgress) -> None:
            # Each window notifies "optimizing" then "testing"; count once per window
            # (on "testing", when its optimization is done) to advance the global bar.
            if progress.phase != "testing":
                return
            genetic_staging.bump_completed_windows(run_id)
            _persist_genetic_progress(
                run_id,
                db_run_id,
                request,
                generation=generation,
                windows_per_candidate=windows_per_candidate,
                candidate_id=candidate_id,
            )

        try:
            result = evaluate_candidate(
                candidate,
                _config_for_walkforward(request),
                _candidate_runner(request),
                ohlcv=_candidate_ohlcv(request),
                progress_callback=on_window,
                should_stop=lambda: is_cancelled(run_id),
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - isolate a single candidate failure
            logger.exception(
                "Genetic candidate %s failed for run %s", genome.genome_id, run_id
            )
            result = CandidateResult(
                candidate_id=genome.genome_id,
                strategy="CompositeStrategy",
                status="error",
                error=str(exc),
            )
        stash_partial(run_id, candidate_index, candidate_result_to_dict(result))
        genetic_staging.set_candidate_meta(
            run_id,
            {
                genome.genome_id: _genetic_staged_metadata(
                    genome,
                    generation=generation,
                    genetic=genetic,
                )
            },
        )

    remaining = decrement(run_id)
    _persist_genetic_progress(
        run_id,
        db_run_id,
        request,
        generation=generation,
        windows_per_candidate=windows_per_candidate,
        candidate_id=candidate_id,
    )
    if remaining <= 0:
        finalize_generation(run_id, db_run_id_hex, config_json, generation)


def finalize_generation(
    run_id: str, db_run_id_hex: str, config_json: str, generation: int
) -> None:
    """Last worker of a generation: breed the next one, or finalize the whole run."""
    request = StrategySearchConfig.model_validate_json(config_json)
    genetic = request.genetic
    assert genetic is not None

    # Reload the provider state stashed at dispatch (population == this generation),
    # so result→genome mapping and breeding are deterministic regardless of which
    # worker is last and the arbitrary order partials come back in. The probe frame
    # re-enables mutation repair when breeding the next generation in ``report()``.
    provider = create_genetic_candidate_provider(
        genetic,
        request,
        probe_df=_genetic_probe_frame(request),
        latents_enabled=request.latents_enabled,
    )
    provider.load_state(genetic_staging.get_provider_state(run_id))

    results_by_id = {
        result.candidate_id: result
        for result in (candidate_result_from_dict(p) for p in load_partials(run_id))
    }
    ordered = [
        results_by_id[genome.genome_id]
        for genome in provider.population
        if genome.genome_id in results_by_id
    ]
    genetic_staging.append_generation_results(
        run_id, generation, [candidate_result_to_dict(r) for r in ordered]
    )

    cancelled = is_cancelled(run_id)
    next_generation = generation + 1
    if not cancelled and next_generation < genetic.generations:
        provider.report(ordered)
        _dispatch_generation(
            run_id, db_run_id_hex, config_json, provider, next_generation
        )
        return

    _finalize_genetic(run_id, db_run_id_hex, config_json, cancelled=cancelled)


def _finalize_genetic(
    run_id: str, db_run_id_hex: str, config_json: str, *, cancelled: bool
) -> None:
    """Rank across generations, run DSR + lock-box, persist results, mark terminal."""
    request = StrategySearchConfig.model_validate_json(config_json)
    db_run_id = uuid.UUID(db_run_id_hex) if db_run_id_hex else None
    assert request.genetic is not None
    job = StrategySearchJob(
        run_id=run_id,
        db_run_id=db_run_id,
        request=request,
        total_candidates=_resolve_total_candidates(
            request,
            create_genetic_candidate_provider(
                request.genetic, request, latents_enabled=request.latents_enabled
            ),
        ),
        total_generations=request.genetic.generations,
    )

    terminal_status: JobStatus = "failed"
    try:
        all_generations = [
            [candidate_result_from_dict(p) for p in generation]
            for generation in genetic_staging.get_all_generation_results(run_id)
        ]
        metadata = genetic_staging.get_all_candidate_meta(run_id)

        # The orchestrator's finalizer (ranking, DSR, lock-box, summary) is reused
        # verbatim by injecting the accumulated generations and metadata into a shell
        # instance — the math is identical to the in-process path.
        provider = GeneticCandidateProvider(request.genetic, request)
        orchestrator = GeneticStrategySearchOrchestrator(
            request, provider, _candidate_runner(request)
        )
        orchestrator._generations = all_generations
        orchestrator._candidate_metadata = metadata
        job.result = orchestrator.finalize()
        job.total_candidates = len(job.result.candidates)
        job.lake_paths = _write_lake_artifacts(run_id, job.result)
        if cancelled:
            terminal_status = "cancelled"
            job.error = "Cancelled by user"
        else:
            terminal_status = "completed"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Genetic discovery finalize failed for run %s", run_id)
        terminal_status = "failed"
        job.error = str(exc)
    finally:
        job.updated_at = _now()
        job.status = terminal_status
        _persist_run_finish(job, terminal_status)
        _persist_progress(job)
        clear_partials(run_id)
        genetic_staging.clear_genetic_keys(run_id)
        clear_job_keys(run_id)


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
        from q_backend.optimization.hypothesis import resolve_candidate_provider
        provider = resolve_candidate_provider(request)
        provider_metadata = {}
        if hasattr(provider, "candidate_metadata"):
            provider_metadata = provider.candidate_metadata()
        job.result = StrategySearchResult(
            candidates=ranked,
            objective_mode=request.objective.mode,
            best=best,
            candidate_metadata=provider_metadata or None,
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
    config = _serialize_search_config(job.request)
    payload: dict[str, Any] = {
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
    if job.generation is not None:
        payload["generation"] = job.generation
    if job.total_generations is not None:
        payload["total_generations"] = job.total_generations
    return payload


def _serialize_candidate(
    candidate: CandidateResult,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    if metadata:
        if metadata.get("generation") is not None:
            payload["generation"] = metadata["generation"]
        if metadata.get("genome") is not None:
            payload["genome"] = metadata["genome"]
        if metadata.get("genome_node_count") is not None:
            payload["genome_node_count"] = metadata["genome_node_count"]
        if metadata.get("dsr") is not None:
            payload["dsr"] = metadata["dsr"]
        if metadata.get("complexity_penalty") is not None:
            payload["complexity_penalty"] = metadata["complexity_penalty"]
        if metadata.get("exit_preset_id") is not None:
            payload["exit_preset_id"] = metadata["exit_preset_id"]
        if metadata.get("exit_preset_label") is not None:
            payload["exit_preset_label"] = metadata["exit_preset_label"]
        if metadata.get("exit_param_names") is not None:
            payload["exit_param_names"] = metadata["exit_param_names"]
        if metadata.get("exit_policy_id") is not None:
            payload["exit_policy_id"] = metadata["exit_policy_id"]
        if metadata.get("exit_policy_label") is not None:
            payload["exit_policy_label"] = metadata["exit_policy_label"]
        if metadata.get("last_exit_mutation_op") is not None:
            payload["last_exit_mutation_op"] = metadata["last_exit_mutation_op"]
    _attach_exit_quality_fields(
        payload,
        diagnostics=candidate.diagnostics,
    )
    return payload


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
    metadata = result.candidate_metadata or {}
    return {
        "run_id": job.run_id,
        "status": job.status,
        "objective_mode": result.objective_mode.value,
        "summary": summary,
        "candidates": [
            _serialize_candidate(
                candidate,
                metadata=metadata.get(candidate.candidate_id),
            )
            for candidate in result.candidates
        ],
        "best": (
            _serialize_candidate(
                result.best,
                metadata=metadata.get(result.best.candidate_id),
            )
            if result.best is not None
            else None
        ),
        "search_config": _serialize_search_config(job.request),
        "lake_paths": job.lake_paths,
    }


def _serialize_db_candidate(candidate, *, run_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
        "profile_version": getattr(candidate, "profile_version", None),
        "hypothesis_id": getattr(candidate, "hypothesis_id", None),
        "hypothesis_rationale": getattr(candidate, "hypothesis_rationale", None),
        "hypothesis_required_features": getattr(candidate, "hypothesis_required_features", None),
        "hypothesis_template_hash": getattr(candidate, "hypothesis_template_hash", None),
    }
    if candidate.generation is not None:
        payload["generation"] = candidate.generation
    if candidate.genome is not None:
        payload["genome"] = candidate.genome
    if candidate.genome_node_count is not None:
        payload["genome_node_count"] = candidate.genome_node_count
    if candidate.dsr is not None:
        payload["dsr"] = candidate.dsr
    if candidate.complexity_penalty is not None:
        payload["complexity_penalty"] = candidate.complexity_penalty
    if candidate.exit_preset_id is not None:
        payload["exit_preset_id"] = candidate.exit_preset_id
    if candidate.exit_preset_label is not None:
        payload["exit_preset_label"] = candidate.exit_preset_label
    if candidate.exit_param_names is not None:
        payload["exit_param_names"] = candidate.exit_param_names
    if candidate.exit_policy_id is not None:
        payload["exit_policy_id"] = candidate.exit_policy_id
    if candidate.exit_policy_label is not None:
        payload["exit_policy_label"] = candidate.exit_policy_label
    if candidate.last_exit_mutation_op is not None:
        payload["last_exit_mutation_op"] = candidate.last_exit_mutation_op
    _attach_exit_quality_fields(
        payload,
        diagnostics=candidate.diagnostics,
        run_id=run_id,
        candidate_id=candidate.candidate_id,
    )
    return payload


def status_payload_from_db(run_id: str) -> dict[str, Any] | None:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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


def _get_redis_logs(run_id: str) -> list[str]:
    try:
        redis_client = get_redis()
        log_key = f"strategy_search:logs:{run_id}"
        logs = redis_client.lrange(log_key, 0, -1)
        if logs:
            return [log.decode("utf-8") if isinstance(log, bytes) else log for log in logs]
    except Exception as e:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning("Failed to retrieve trial logs from Redis for run %s: %s", run_id, e)
    return []


def get_status_payload(run_id: str) -> dict[str, Any] | None:
    db_payload = status_payload_from_db(run_id)

    try:
        cached = get_job_progress(get_redis(), run_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.debug(
            "Redis progress read unavailable for strategy search run %s", run_id
        )
        cached = None

    # While the run is still active, candidate results aren't in the DB yet (they
    # are written by the finalizer), so the live Redis snapshot is authoritative.
    # Once terminal, the DB row is complete and wins.
    if db_payload is None:
        if cached is not None:
            cached["logs"] = _get_redis_logs(run_id)
        return cached
    if db_payload.get("status") in ("pending", "running") and cached is not None:
        cached["logs"] = _get_redis_logs(run_id)
        return cached
    db_payload["logs"] = _get_redis_logs(run_id)
    return db_payload


def get_persisted_run_status(run_id: str) -> Optional[str]:
    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None
    try:
        with session_scope() as session:
            run = get_strategy_search_run(session, run_uuid)
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning("Failed to load strategy search run %s from DB: %s", run_id, exc)
        return None

    if run is None:
        return None

    if run.status not in _FINISHED_STATUSES:
        return None

    candidates = sorted(
        run.candidates, key=lambda item: (item.rank or 10_000, item.candidate_id)
    )
    serialized = [
        _serialize_db_candidate(candidate, run_id=run_id) for candidate in candidates
    ]
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
    except Exception as exc:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        logger.warning(
            "Failed to delete strategy search lake artifacts for %s: %s", run_id, exc
        )


def candidate_exists_in_run(run_id: str, candidate_id: str) -> bool:
    job = get_job(run_id)
    if job is not None and job.result is not None:
        metadata = job.result.candidate_metadata or {}
        if candidate_id in metadata:
            return True
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
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        return False

    if run is None:
        return False
    return any(candidate.candidate_id == candidate_id for candidate in run.candidates)


def get_candidate_genome(run_id: str, candidate_id: str) -> dict[str, Any] | None:
    job = get_job(run_id)
    if job is not None and job.result is not None:
        metadata = (job.result.candidate_metadata or {}).get(candidate_id)
        if metadata and metadata.get("genome") is not None:
            return metadata["genome"]

    try:
        run_uuid = _parse_run_uuid(run_id)
    except ValueError:
        return None

    try:
        with session_scope() as session:
            candidate = get_strategy_search_candidate(
                session, run_id=run_uuid, candidate_id=candidate_id
            )
    except Exception:  # noqa: BLE001 - best-effort persistence/lake/progress; logged and degraded
        candidate = None

    if candidate is not None and candidate.genome is not None:
        return candidate.genome

    try:
        return read_strategy_search_candidate_genome(run_id, candidate_id)
    except FileNotFoundError:
        return None
