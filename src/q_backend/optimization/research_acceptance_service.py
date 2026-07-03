"""Orchestration for repeated-seed research acceptance (WO163)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import pandas as pd

from q_backend.features.split_manifest import SplitManifest
from q_backend.optimization.backtest_runner import BacktestRunConfig, BacktestRunner
from q_backend.optimization.lockbox import evaluate_lockbox as run_lockbox_evaluation
from q_backend.optimization.lockbox_consumption import (
    LockboxConsumptionState,
    assert_lockbox_available,
    check_lockbox_consumption,
    record_lockbox_consumption,
)
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.research_acceptance import (
    AttemptCountInputs,
    PlateauNeighborResult,
    PlateauResult,
    ResearchAcceptanceConfig,
    ResearchAcceptanceResult,
    SeedRunRecord,
    compute_champion_hash,
    compute_dsr_value,
    compute_effective_attempt_count,
    compute_tail_diagnostics,
    evaluate_plateau_result,
    evaluate_research_acceptance_from_evidence,
    generate_parameter_neighbors,
    select_champion_seed,
    window_returns_from_results,
)
from q_backend.optimization.search_space import build_position_sizing_config
from q_backend.optimization.strategy_search import (
    SearchCandidate,
    StrategySearchConfig,
    _build_optimization_config,
    _runner_for_candidate,
)
from q_backend.optimization.walkforward import WalkForwardRunner
from q_backend.storage.lake.artifacts import (
    write_research_acceptance_result,
    write_research_acceptance_seed,
)

logger = logging.getLogger(__name__)


def _default_seed_list(config: ResearchAcceptanceConfig, base_seed: int) -> list[int]:
    return [base_seed + index for index in range(config.optimization_seeds)]


def evaluate_seed_run(
    *,
    candidate: SearchCandidate,
    config: StrategySearchConfig,
    backtest_runner: BacktestRunner,
    seed: int,
    ohlcv: pd.DataFrame | None = None,
) -> SeedRunRecord:
    """Run one walk-forward optimization seed and capture OOS evidence."""
    study = config.study.model_copy(deep=True)
    study.seed = seed
    run_config = config.model_copy(deep=True)
    run_config.study = study

    opt_config = _build_optimization_config(candidate, run_config)
    runner = _runner_for_candidate(backtest_runner, candidate.fixed_params)
    record = SeedRunRecord(seed=seed, status="failed", study_config=study.model_dump())

    try:
        wf_result = WalkForwardRunner(
            opt_config,
            run_config.walkforward,
            runner,
            ohlcv=ohlcv,
            candidate_id=candidate.candidate_id,
        ).run()
    except Exception as exc:  # noqa: BLE001 - per-seed failure is recorded/reported
        # Already-handled: one seed's walk-forward failing must not abort the
        # multi-seed acceptance run. The reason is captured on the record
        # (status stays "failed" + failure_reason); log so the traceback is kept.
        logger.warning(
            "Research acceptance seed %s failed: %s", seed, exc, exc_info=True
        )
        record.failure_reason = str(exc)
        return record

    record.window_count = len(wf_result.windows)
    record.completed_windows = sum(
        1 for window in wf_result.windows if window.status == "completed"
    )
    record.window_returns = window_returns_from_results(
        wf_result.windows,
        mode=run_config.objective.mode,
    )

    if not wf_result.oos_metrics:
        record.failure_reason = "no out-of-sample metrics"
        return record

    record.oos_metrics = wf_result.oos_metrics
    if int(wf_result.oos_metrics.get("total_trades", 0)) == 0:
        record.failure_reason = "zero out-of-sample trades"
        return record

    mode = run_config.objective.mode
    record.objective_value = float(resolve_objective(wf_result.oos_metrics, mode))

    best_window = None
    best_score = float("-inf")
    for window in wf_result.windows:
        if window.status != "completed" or not window.best_params:
            continue
        if not window.oos_metrics:
            continue
        score = float(resolve_objective(window.oos_metrics, mode))
        if score > best_score:
            best_score = score
            best_window = window
    if best_window is not None:
        record.best_params = best_window.best_params

    record.status = "completed"
    return record


def evaluate_neighbor_on_segment(
    *,
    neighbor_params: dict[str, Any],
    candidate: SearchCandidate,
    config: StrategySearchConfig,
    backtest_runner: BacktestRunner,
    segment_start: datetime,
    segment_end: datetime,
) -> tuple[float | None, bool]:
    """Backtest one neighbor on the walk-forward segment only (no lock-box)."""
    backtest = config.backtest.model_copy(deep=True)
    strategy_params = {
        **candidate.fixed_params,
        **neighbor_params.get("strategy_params", {}),
    }
    strategy_params = {
        key: value
        for key, value in strategy_params.items()
        if key != "_exit_preset_id"
    }
    risk_params = neighbor_params.get("risk_params", {})
    position_sizing = build_position_sizing_config(risk_params)

    run_config = BacktestRunConfig(
        symbol=backtest.symbol,
        timeframe=backtest.timeframe,
        start=segment_start,
        end=segment_end,
        initial_capital=backtest.initial_capital,
        point_value=backtest.point_value,
        strategy=candidate.strategy,
        strategy_params=strategy_params,
        position_sizing=position_sizing,
        costs=backtest.costs,
        parallel_mode=backtest.parallel_mode,
        day_trade=backtest.day_trade,
        day_trade_start_time=backtest.day_trade_start_time,
        day_trade_end_time=backtest.day_trade_end_time,
        day_trade_close_time=backtest.day_trade_close_time,
        engine=backtest.engine,
        display_timeframe=backtest.display_timeframe,
        tick_flags=backtest.tick_flags,
    )
    runner = _runner_for_candidate(backtest_runner, candidate.fixed_params)
    result = runner.run(run_config)
    metrics = result.metrics or {}
    if int(metrics.get("total_trades", 0)) == 0:
        return None, False
    objective = float(resolve_objective(metrics, config.objective.mode))
    profitable = float(metrics.get("total_return_pct", 0.0)) > 0.0
    return objective, profitable


def evaluate_parameter_plateau(
    *,
    champion_params: dict[str, Any],
    champion_objective: float | None,
    candidate: SearchCandidate,
    config: StrategySearchConfig,
    acceptance_config: ResearchAcceptanceConfig,
    backtest_runner: BacktestRunner,
    segment_start: datetime,
    segment_end: datetime,
) -> PlateauResult:
    neighbors = generate_parameter_neighbors(
        champion_params,
        candidate.search_space,
        max_neighbors=acceptance_config.max_neighbors,
    )
    neighbor_results: list[PlateauNeighborResult] = []
    for neighbor in neighbors:
        objective, profitable = evaluate_neighbor_on_segment(
            neighbor_params=neighbor,
            candidate=candidate,
            config=config,
            backtest_runner=backtest_runner,
            segment_start=segment_start,
            segment_end=segment_end,
        )
        neighbor_results.append(
            PlateauNeighborResult(
                params=neighbor,
                objective_value=objective,
                profitable=profitable,
            )
        )
    return evaluate_plateau_result(
        champion_objective=champion_objective,
        neighbors=neighbor_results,
    )


def run_research_acceptance(
    *,
    candidate: SearchCandidate,
    config: StrategySearchConfig,
    acceptance_config: ResearchAcceptanceConfig,
    backtest_runner: BacktestRunner,
    manifest: SplitManifest | None = None,
    attempt_inputs: AttemptCountInputs | None = None,
    seeds: list[int] | None = None,
    ohlcv: pd.DataFrame | None = None,
    acceptance_id: str | None = None,
    persist: bool = True,
    run_lockbox: bool = True,
) -> ResearchAcceptanceResult:
    """Run repeated seeds, plateau, DSR, and optional lock-box acceptance."""
    acceptance_id = acceptance_id or str(uuid.uuid4())
    seed_list = seeds or _default_seed_list(acceptance_config, config.study.seed)

    seed_records: list[SeedRunRecord] = []
    for seed in seed_list:
        record = evaluate_seed_run(
            candidate=candidate,
            config=config,
            backtest_runner=backtest_runner,
            seed=seed,
            ohlcv=ohlcv,
        )
        seed_records.append(record)
        if persist:
            write_research_acceptance_seed(acceptance_id, seed, record.to_dict())

    champion = select_champion_seed(seed_records)
    champion_params = champion.best_params if champion else None
    champion_hash = None
    if champion_params is not None:
        champion_hash = compute_champion_hash(
            candidate_id=candidate.candidate_id,
            champion_params=champion_params,
        )

    segment_start = manifest.walkforward.start if manifest else config.backtest.start
    segment_end = manifest.walkforward.end if manifest else config.backtest.end

    plateau: PlateauResult | None = None
    if champion is not None and champion_params is not None:
        plateau = evaluate_parameter_plateau(
            champion_params=champion_params,
            champion_objective=champion.objective_value,
            candidate=candidate,
            config=config,
            acceptance_config=acceptance_config,
            backtest_runner=backtest_runner,
            segment_start=segment_start,
            segment_end=segment_end,
        )

    attempt_inputs = attempt_inputs or AttemptCountInputs(
        hypothesis_count=1,
        optimization_seeds=len(seed_list),
        optuna_trials=config.study.n_trials * len(seed_list),
    )
    effective_attempt_count = compute_effective_attempt_count(attempt_inputs)

    dsr_value = None
    if champion is not None and champion.oos_metrics is not None:
        dsr_value = compute_dsr_value(
            oos_metrics=champion.oos_metrics,
            oos_equity_curve=None,
            attempt_count=effective_attempt_count,
        )

    manifest_hash = manifest.manifest_hash if manifest else None
    lockbox_metrics: dict[str, Any] | None = None
    lockbox_evaluated = False
    lockbox_blocked = False

    if run_lockbox and config.lockbox.enabled and champion_params is not None:
        if manifest_hash is not None and champion_hash is not None:
            consumption = check_lockbox_consumption(manifest_hash, champion_hash)
            if consumption.state == LockboxConsumptionState.MANIFEST_CONSUMED:
                lockbox_blocked = True
            elif consumption.state == LockboxConsumptionState.SAME_CHAMPION:
                lockbox_evaluated = True
                lockbox_metrics = consumption.record.lockbox_metrics if consumption.record else None
            else:
                assert_lockbox_available(manifest_hash, champion_hash)
                lockbox_params = dict(champion_params)
                genome = candidate.fixed_params.get("genome")
                if genome is not None:
                    strategy_params = dict(lockbox_params.get("strategy_params", {}))
                    strategy_params["genome"] = genome
                    lockbox_params["strategy_params"] = strategy_params
                lockbox_metrics, _, _ = run_lockbox_evaluation(
                    backtest=config.backtest,
                    lockbox=config.lockbox,
                    best_params=lockbox_params,
                    strategy=candidate.strategy,
                    backtest_runner=backtest_runner,
                )
                lockbox_evaluated = True
        else:
            lockbox_params = dict(champion_params)
            genome = candidate.fixed_params.get("genome")
            if genome is not None:
                strategy_params = dict(lockbox_params.get("strategy_params", {}))
                strategy_params["genome"] = genome
                lockbox_params["strategy_params"] = strategy_params
            lockbox_metrics, _, _ = run_lockbox_evaluation(
                backtest=config.backtest,
                lockbox=config.lockbox,
                best_params=lockbox_params,
                strategy=candidate.strategy,
                backtest_runner=backtest_runner,
            )
            lockbox_evaluated = True

    tail = compute_tail_diagnostics(
        window_returns=champion.window_returns if champion else [],
    )

    result = evaluate_research_acceptance_from_evidence(
        acceptance_id=acceptance_id,
        candidate_id=candidate.candidate_id,
        config=acceptance_config,
        seeds=seed_records,
        plateau=plateau,
        dsr_value=dsr_value,
        effective_attempt_count=effective_attempt_count,
        lockbox_metrics=lockbox_metrics,
        lockbox_evaluated=lockbox_evaluated,
        lockbox_blocked=lockbox_blocked,
        champion_hash=champion_hash,
        manifest_hash=manifest_hash,
        tail=tail,
    )

    if (
        persist
        and run_lockbox
        and lockbox_evaluated
        and not lockbox_blocked
        and manifest_hash is not None
        and champion_hash is not None
        and lockbox_metrics is not None
    ):
        record_lockbox_consumption(
            manifest_hash=manifest_hash,
            champion_hash=champion_hash,
            lockbox_metrics=lockbox_metrics,
            verdict=result.verdict,
            candidate_id=candidate.candidate_id,
        )

    if persist:
        write_research_acceptance_result(acceptance_id, result.to_dict())

    return result
