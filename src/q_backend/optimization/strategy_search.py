"""Automatic strategy search: sweep candidates, walk-forward validate, rank on OOS.

Each candidate is optimized in-sample per walk-forward window and scored on
stitched out-of-sample metrics. Bounded registry params become search dimensions
(WO30); unbounded params are pinned via ``fixed_params`` merged into every
backtest run by ``_FixedParamsBacktestRunner``.

The ``CandidateProvider`` protocol is the extension seam — ``RegistryCandidateProvider``
sweeps registered candle strategies today; a future genetic engine will implement
the same protocol and reuse ``evaluate_candidate`` unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from q_backend.backtesting.strategy_registry import (
    StrategyInfo,
    get_registered_strategy,
    list_registered_strategies,
)
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.strategy_registry import ExitPreset
from q_backend.optimization.auto_search_space import (
    auto_search_space,
    derive_strategy_search_space,
)
from q_backend.optimization.exit_preset_search_space import (
    derive_exit_preset_search_space,
    preset_exit_param_names,
)
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
    BacktestRunner,
)
from q_backend.optimization.models import (
    BacktestConfig,
    ObjectiveConfig,
    ObjectiveMode,
    OptimizationConfig,
    SearchSpaceConfig,
    StorageConfig,
    StudyConfig,
)
from q_backend.optimization.objectives import resolve_objective
from q_backend.backtesting.models import Trade
from q_backend.optimization.exit_quality import score_exit_quality, summarize_exit_quality
from q_backend.market_data.exogenous_config import (
    ExogenousSeriesConfig,
    validate_exogenous_for_primary,
)
from q_backend.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardProgress,
    WalkForwardResult,
    WalkForwardRunner,
    WalkForwardWindowResult,
)

logger = logging.getLogger(__name__)


class GateConfig(BaseModel):
    min_completed_windows: int = Field(default=2, ge=1)
    min_oos_trades: int = Field(default=10, ge=0)
    efficiency_low: float = 0.3
    efficiency_high: float = 1.5


class LockboxConfig(BaseModel):
    enabled: bool = False
    lockbox_pct: float | None = 0.15
    lockbox_days: int | None = None
    min_trades: int = 5
    max_drawdown_pct: float | None = None

    @model_validator(mode="after")
    def validate_lockbox_size(self) -> LockboxConfig:
        if self.enabled and self.lockbox_pct is not None and self.lockbox_days is not None:
            raise ValueError("lockbox_pct and lockbox_days are mutually exclusive")
        return self


class ExitPresetSearchConfig(BaseModel):
    enabled: bool = False
    preset_ids: list[str] | None = None
    include_baseline: bool = True
    pin_non_preset_exits_off: bool = True


class ExitQualityScoringConfig(BaseModel):
    enabled: bool = False
    min_mfe_capture_ratio: float | None = None
    max_profit_giveback_pct: float | None = None


class GeneticSearchConfig(BaseModel):
    population_size: int = Field(default=48, ge=10, le=200)
    generations: int = Field(default=12, ge=2, le=50)
    elite_count: int = Field(default=4, ge=1)
    crossover_rate: float = Field(default=0.7, ge=0.0, le=1.0)
    mutation_rate: float = Field(default=0.15, ge=0.0, le=1.0)
    tournament_size: int = Field(default=3, ge=2)
    init_seed: int | None = None
    max_nodes: int = Field(default=24, ge=4)
    max_depth: int = Field(default=12, ge=3)
    complexity_lambda: float = 0.001
    complexity_mu: float = 0.0005
    max_workers: int | None = Field(default=None, ge=1)
    # Selection-signal shaping (does not change gates or the reported leaderboard).
    gate_penalty_efficiency: float = 0.5
    gate_penalty_trades: float = 0.5
    gate_penalty_windows: float = 0.5
    no_result_floor: float = -2.0
    error_floor: float = -4.0
    prescreen_min_signals: int = Field(default=1, ge=0)
    min_seed_signals: int = Field(default=1, ge=0)
    repair_max_attempts: int = Field(default=8, ge=1)
    mutation_rate_min: float = Field(default=0.10, ge=0.0, le=1.0)
    mutation_rate_max: float = Field(default=0.50, ge=0.0, le=1.0)
    stagnation_patience: int = Field(default=2, ge=1)
    adaptive_operator_weights: bool = True
    seed_exit_policies: bool = True
    exit_policy_preset_ids: list[str] | None = None
    exit_policy_seed_fraction: float = Field(default=0.25, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_elite_count(self) -> GeneticSearchConfig:
        if self.elite_count >= self.population_size:
            raise ValueError("elite_count must be less than population_size")
        if self.mutation_rate_min > self.mutation_rate_max:
            raise ValueError("mutation_rate_min must be <= mutation_rate_max")
        if self.exit_policy_preset_ids is not None:
            from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS

            known = {preset.id for preset in EXIT_PRESETS}
            unknown = [preset_id for preset_id in self.exit_policy_preset_ids if preset_id not in known]
            if unknown:
                raise ValueError(f"Unknown exit policy preset id(s): {', '.join(unknown)}")
        return self


class StrategySearchConfig(BaseModel):
    backtest: BacktestConfig
    objective: ObjectiveConfig
    walkforward: WalkForwardConfig
    study: StudyConfig
    strategies: list[str] | None = None
    include_risk_search: bool = True
    gates: GateConfig = Field(default_factory=GateConfig)
    genetic: GeneticSearchConfig | None = None
    lockbox: LockboxConfig = Field(default_factory=LockboxConfig)
    exit_presets: ExitPresetSearchConfig = Field(default_factory=ExitPresetSearchConfig)
    exit_quality_scoring: ExitQualityScoringConfig = Field(default_factory=ExitQualityScoringConfig)
    # Internal harness override (WO153): when False, discovery ignores any PRODUCTION
    # model and runs latents-OFF. Defaults True so normal discovery stays automatic.
    # Not part of the frontend discovery request surface (WO156/157 use a separate
    # Experiments API).
    latents_enabled: bool = True
    exogenous_series: list[ExogenousSeriesConfig] = Field(default_factory=list)
    exogenous_provenance: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_exogenous_context(self) -> StrategySearchConfig:
        validate_exogenous_for_primary(
            primary_symbol=self.backtest.symbol,
            primary_timeframe=self.backtest.timeframe,
            exogenous_series=self.exogenous_series,
        )
        return self

    @model_validator(mode="after")
    def reject_multi_objective(self) -> StrategySearchConfig:
        if self.objective.mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN:
            raise ValueError(
                "Strategy search requires a single-objective mode; " "multi-objective ranking is not supported"
            )
        return self

    @model_validator(mode="after")
    def reject_exit_presets_with_genetic(self) -> StrategySearchConfig:
        if self.genetic is not None and self.exit_presets.enabled:
            raise ValueError(
                "exit_presets is not supported with genetic search; " "genetic exit-policy evolution is WO80"
            )
        if self.exit_presets.enabled and self.exit_presets.preset_ids is not None:
            known = {preset.id for preset in EXIT_PRESETS}
            unknown = [preset_id for preset_id in self.exit_presets.preset_ids if preset_id not in known]
            if unknown:
                raise ValueError(f"Unknown exit preset id(s): {', '.join(unknown)}")
        return self


METADATA_FIXED_PARAM_KEYS = frozenset({"_exit_preset_id"})


@dataclass(frozen=True)
class SearchCandidate:
    candidate_id: str
    strategy: str
    search_space: SearchSpaceConfig
    fixed_params: dict[str, Any]


@dataclass
class CandidateResult:
    candidate_id: str
    strategy: str
    status: Literal["completed", "no_result", "unsupported", "error"]
    rank: int | None = None
    objective_value: float | None = None
    robustness_score: float | None = None
    efficiency: float | None = None
    gate_flags: list[str] = field(default_factory=list)
    passed_gates: bool = False
    oos_metrics: dict[str, Any] | None = None
    is_metrics_summary: dict[str, Any] | None = None
    best_params: dict[str, Any] | None = None
    window_count: int = 0
    completed_windows: int = 0
    oos_equity_curve: pd.Series | None = None
    oos_trades: list[Trade] = field(default_factory=list)
    diagnostics: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class GeneticFinalizeSummary:
    generations_completed: int = 0
    total_genomes_evaluated: int = 0
    champion_dsr: float | None = None
    n_trials_effective: int = 0
    sr_observed: float | None = None
    lockbox_metrics: dict[str, Any] | None = None
    lockbox_passed: bool | None = None
    lockbox_equity_curve: pd.Series | None = None


@dataclass
class StrategySearchResult:
    candidates: list[CandidateResult]
    objective_mode: ObjectiveMode
    best: CandidateResult | None
    genetic_summary: GeneticFinalizeSummary | None = None
    all_generations: list[list[CandidateResult]] | None = None
    candidate_metadata: dict[str, dict[str, Any]] | None = None


@dataclass
class SearchProgress:
    current_candidate: int
    total_candidates: int
    candidate_id: str
    strategy: str
    phase: Literal["optimizing", "testing", "done"]
    window_index: int | None
    total_windows: int | None
    generation: int | None = None
    total_generations: int | None = None


class CandidateProvider(Protocol):
    def candidates(self) -> Iterable[SearchCandidate]: ...

    def report(self, results: list[CandidateResult]) -> None: ...


@dataclass
class _FixedParamsBacktestRunner:
    """Merge WO30 ``fixed_params`` into every backtest ``strategy_params`` dict."""

    inner: BacktestRunner
    fixed_strategy_params: dict[str, Any]

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        merged = {**self.fixed_strategy_params, **config.strategy_params}
        return self.inner.run(replace(config, strategy_params=merged))


def _selected_exit_presets(config: ExitPresetSearchConfig) -> list[ExitPreset]:
    if config.preset_ids is None:
        return list(EXIT_PRESETS)
    by_id = {preset.id: preset for preset in EXIT_PRESETS}
    return [by_id[preset_id] for preset_id in config.preset_ids]


class RegistryCandidateProvider:
    """Yield one ``SearchCandidate`` per selected candle strategy via WO30."""

    def __init__(self, config: StrategySearchConfig) -> None:
        self._config = config
        self._unsupported_names: list[str] = []
        self._candidate_metadata: dict[str, dict[str, Any]] = {}

    def candidates(self) -> Iterable[SearchCandidate]:
        exit_cfg = self._config.exit_presets
        presets = _selected_exit_presets(exit_cfg) if exit_cfg.enabled else []

        for info in self._selected_infos():
            if info.engine == "tick":
                self._unsupported_names.append(info.name)
                continue

            if not exit_cfg.enabled:
                yield self._baseline_candidate(info)
                continue

            if exit_cfg.include_baseline:
                yield self._baseline_candidate(info)

            for preset in presets:
                yield self._exit_preset_candidate(info, preset)

    def candidate_metadata(self) -> dict[str, dict[str, Any]]:
        if self._config.exit_presets.enabled and not self._candidate_metadata:
            list(self.candidates())
        return dict(self._candidate_metadata)

    def unsupported_names(self) -> list[str]:
        return list(self._unsupported_names)

    def report(self, results: list[CandidateResult]) -> None:
        return None

    def _baseline_candidate(self, info: StrategyInfo) -> SearchCandidate:
        search_space = auto_search_space(
            info.name,
            include_risk=self._config.include_risk_search,
        )
        _, fixed_params = derive_strategy_search_space(info.name)
        return SearchCandidate(
            candidate_id=info.name,
            strategy=info.name,
            search_space=search_space,
            fixed_params=fixed_params,
        )

    def _exit_preset_candidate(self, info: StrategyInfo, preset: ExitPreset) -> SearchCandidate:
        exit_cfg = self._config.exit_presets
        candidate_id = f"{info.name}__exit_{preset.id}"
        search_space, fixed_params = derive_exit_preset_search_space(
            info.name,
            preset,
            include_risk=self._config.include_risk_search,
            pin_non_preset_exits_off=exit_cfg.pin_non_preset_exits_off,
        )
        fixed_params = {**fixed_params, "_exit_preset_id": preset.id}
        exit_param_names = preset_exit_param_names(preset)
        self._candidate_metadata[candidate_id] = {
            "exit_preset_id": preset.id,
            "exit_preset_label": preset.label,
            "exit_param_names": exit_param_names,
        }
        return SearchCandidate(
            candidate_id=candidate_id,
            strategy=info.name,
            search_space=search_space,
            fixed_params=fixed_params,
        )

    def _selected_infos(self) -> list[StrategyInfo]:
        if self._config.strategies is None:
            return list_registered_strategies()
        infos: list[StrategyInfo] = []
        for name in self._config.strategies:
            infos.append(get_registered_strategy(name).info)
        return infos


def _strategy_fixed_params(fixed_params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fixed_params.items() if key not in METADATA_FIXED_PARAM_KEYS}


def _runner_for_candidate(
    backtest_runner: BacktestRunner,
    fixed_params: dict[str, Any],
) -> BacktestRunner:
    strategy_params = _strategy_fixed_params(fixed_params)
    if not strategy_params:
        return backtest_runner
    return _FixedParamsBacktestRunner(
        inner=backtest_runner,
        fixed_strategy_params=strategy_params,
    )


def _build_optimization_config(
    candidate: SearchCandidate,
    config: StrategySearchConfig,
) -> OptimizationConfig:
    study = config.study.model_copy(deep=True)
    study.name = f"{config.study.name}__{candidate.candidate_id}"
    study.storage = StorageConfig(type="memory")

    backtest = config.backtest.model_copy(deep=True)
    backtest.strategy = candidate.strategy

    # Apply matched profile session rules (like day_trade, timings) if applicable
    from q_backend.optimization.hypothesis import match_profile

    profile = match_profile(backtest.symbol, backtest.timeframe)
    if profile is not None:
        rules = profile.session_rules
        backtest.day_trade = rules.day_trade
        backtest.day_trade_start_time = rules.day_trade_start_time
        backtest.day_trade_end_time = rules.day_trade_end_time
        backtest.day_trade_close_time = rules.day_trade_close_time

    return OptimizationConfig(
        study=study,
        objective=config.objective,
        backtest=backtest,
        search_space=candidate.search_space,
    )


def _robustness_score(objective_value: float, mode: ObjectiveMode) -> float:
    if mode == ObjectiveMode.MINIMIZE_DRAWDOWN:
        return -objective_value
    return objective_value


def _is_metrics_summary(
    windows: list[WalkForwardWindowResult],
    mode: ObjectiveMode,
) -> dict[str, Any] | None:
    values: list[float] = []
    for window in windows:
        if window.status != "completed" or not window.is_metrics:
            continue
        values.append(float(resolve_objective(window.is_metrics, mode)))
    if not values:
        return None
    return {"mean_objective": sum(values) / len(values), "window_count": len(values)}


def _best_window(
    windows: list[WalkForwardWindowResult],
    mode: ObjectiveMode,
) -> WalkForwardWindowResult | None:
    completed = [window for window in windows if window.status == "completed" and window.oos_metrics]
    if not completed:
        return None

    def score(window: WalkForwardWindowResult) -> float:
        raw = float(resolve_objective(window.oos_metrics or {}, mode))
        return _robustness_score(raw, mode)

    return max(completed, key=score)


def _apply_gates(
    *,
    completed_windows: int,
    oos_metrics: dict[str, Any],
    efficiency: float | None,
    gates: GateConfig,
    mean_is: float | None = None,
    oos_objective: float | None = None,
) -> tuple[list[str], bool]:
    flags: list[str] = []
    if completed_windows < gates.min_completed_windows:
        flags.append("few_windows")
    oos_trades = int(oos_metrics.get("total_trades", 0))
    if oos_trades < gates.min_oos_trades:
        flags.append("few_oos_trades")
    if efficiency is not None:
        is_overfit = False
        if mean_is is not None and oos_objective is not None:
            if mean_is > 0 and oos_objective <= 0:
                is_overfit = True
            elif mean_is > 0 and oos_objective > 0 and efficiency < gates.efficiency_low:
                is_overfit = True
        else:
            if efficiency < gates.efficiency_low:
                is_overfit = True
        if is_overfit:
            flags.append("low_efficiency")

        is_suspicious = False
        if mean_is is not None and oos_objective is not None:
            if mean_is > 0 and oos_objective > 0 and efficiency > gates.efficiency_high:
                is_suspicious = True
        else:
            if efficiency > gates.efficiency_high:
                is_suspicious = True
        if is_suspicious:
            flags.append("suspicious_efficiency")
    return flags, len(flags) == 0


def _collect_oos_trades(wf_result: WalkForwardResult) -> list[Trade]:
    return [trade for window in wf_result.windows if window.status == "completed" for trade in window.oos_trades]


def _attach_exit_quality_diagnostics(
    result: CandidateResult,
    *,
    oos_trades: list[Trade],
    bars: pd.DataFrame | None,
    scoring: ExitQualityScoringConfig,
) -> None:
    if not oos_trades:
        return

    exit_quality = summarize_exit_quality(oos_trades, bars=bars)
    diagnostics: dict[str, Any] = {"exit_quality": exit_quality}

    if scoring.enabled:
        soft_score = score_exit_quality(
            exit_quality,
            min_mfe_capture_ratio=scoring.min_mfe_capture_ratio,
            max_profit_giveback_pct=scoring.max_profit_giveback_pct,
        )
        if soft_score is not None:
            diagnostics["exit_quality_score"] = soft_score

    result.diagnostics = diagnostics


def _unsupported_result(strategy_name: str) -> CandidateResult:
    return CandidateResult(
        candidate_id=strategy_name,
        strategy=strategy_name,
        status="unsupported",
        error="Tick-engine strategies are not supported for walk-forward search",
    )


def evaluate_candidate(
    candidate: SearchCandidate,
    config: StrategySearchConfig,
    backtest_runner: BacktestRunner,
    ohlcv: pd.DataFrame | None = None,
    progress_callback: Callable[[SearchProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    run_id: str | None = None,
) -> CandidateResult:
    base = CandidateResult(
        candidate_id=candidate.candidate_id,
        strategy=candidate.strategy,
        status="no_result",
    )

    if should_stop is not None and should_stop():
        base.status = "error"
        base.error = "cancelled before evaluation"
        return base

    opt_config = _build_optimization_config(candidate, config)
    runner = _runner_for_candidate(backtest_runner, candidate.fixed_params)

    def wf_progress(wf: WalkForwardProgress) -> None:
        if progress_callback is None:
            return
        progress_callback(
            SearchProgress(
                current_candidate=0,
                total_candidates=0,
                candidate_id=candidate.candidate_id,
                strategy=candidate.strategy,
                phase=wf.phase,
                window_index=wf.window_index,
                total_windows=wf.total_windows,
            )
        )

    try:
        wf_result = WalkForwardRunner(
            opt_config,
            config.walkforward,
            runner,
            ohlcv=ohlcv,
            run_id=run_id,
            candidate_id=candidate.candidate_id,
        ).run(progress_callback=wf_progress, should_stop=should_stop)
    except Exception as exc:  # noqa: BLE001 - per-candidate failure is counted/reported
        # Already-handled: a single candidate's walk-forward blowing up must not
        # abort the sweep. The failure is recorded on the candidate (status +
        # error) and rolls up into the run's failure_reasons tally; log it so the
        # traceback is not lost to the structured status.
        logger.warning(
            "Candidate %s walk-forward failed: %s",
            candidate.candidate_id,
            exc,
            exc_info=True,
        )
        base.status = "error"
        base.error = str(exc)
        return base

    windows = wf_result.windows
    base.window_count = len(windows)
    base.completed_windows = sum(1 for window in windows if window.status == "completed")
    base.oos_equity_curve = wf_result.oos_equity_curve
    base.efficiency = wf_result.efficiency

    if not wf_result.oos_metrics:
        base.error = "no out-of-sample metrics"
        return base

    base.oos_metrics = wf_result.oos_metrics
    oos_trades = int(wf_result.oos_metrics.get("total_trades", 0))
    if oos_trades == 0:
        base.error = "zero out-of-sample trades"
        return base

    mode = config.objective.mode
    base.objective_value = float(resolve_objective(wf_result.oos_metrics, mode))
    base.robustness_score = _robustness_score(base.objective_value, mode)
    base.is_metrics_summary = _is_metrics_summary(windows, mode)

    best = _best_window(windows, mode)
    if best is not None:
        base.best_params = best.best_params

    mean_is = None
    if base.is_metrics_summary is not None:
        mean_is = base.is_metrics_summary.get("mean_objective")

    gate_flags, passed = _apply_gates(
        completed_windows=base.completed_windows,
        oos_metrics=wf_result.oos_metrics,
        efficiency=wf_result.efficiency,
        gates=config.gates,
        mean_is=mean_is,
        oos_objective=base.objective_value,
    )
    base.gate_flags = gate_flags
    base.passed_gates = passed
    base.status = "completed"

    oos_trades = _collect_oos_trades(wf_result)
    base.oos_trades = oos_trades
    _attach_exit_quality_diagnostics(
        base,
        oos_trades=oos_trades,
        bars=ohlcv,
        scoring=config.exit_quality_scoring,
    )
    return base


def _trailing_sort_key(result: CandidateResult) -> tuple[int, str]:
    if not result.passed_gates and result.status == "completed":
        tier = 0
    elif result.status == "no_result":
        tier = 1
    elif result.status == "error":
        tier = 2
    elif result.status == "unsupported":
        tier = 3
    else:
        tier = 4
    return tier, result.candidate_id


def _rank_results(results: list[CandidateResult]) -> list[CandidateResult]:
    passing = [result for result in results if result.passed_gates and result.objective_value is not None]
    passing.sort(
        key=lambda result: result.robustness_score or float("-inf"),
        reverse=True,
    )
    for index, result in enumerate(passing, start=1):
        result.rank = index

    trailing = [result for result in results if result.rank is None]
    trailing.sort(key=_trailing_sort_key)
    return passing + trailing


class StrategySearchRunner:
    def __init__(
        self,
        config: StrategySearchConfig,
        backtest_runner: BacktestRunner,
        provider: CandidateProvider | None = None,
    ) -> None:
        self.config = config
        self.backtest_runner = backtest_runner
        self._provider = provider or RegistryCandidateProvider(config)

    def run(
        self,
        progress_callback: Callable[[SearchProgress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> StrategySearchResult:
        provider = self._provider
        search_candidates = list(provider.candidates())
        unsupported: list[CandidateResult] = []
        if isinstance(provider, RegistryCandidateProvider):
            unsupported = [_unsupported_result(name) for name in provider.unsupported_names()]

        total = len(search_candidates) + len(unsupported)
        results: list[CandidateResult] = []

        for index, unsupported_result in enumerate(unsupported, start=1):
            results.append(unsupported_result)
            if progress_callback is not None:
                progress_callback(
                    SearchProgress(
                        current_candidate=index,
                        total_candidates=total,
                        candidate_id=unsupported_result.candidate_id,
                        strategy=unsupported_result.strategy,
                        phase="done",
                        window_index=None,
                        total_windows=None,
                    )
                )

        ohlcv = getattr(self.backtest_runner, "_df", None)

        candidate_offset = len(unsupported)
        for offset, candidate in enumerate(search_candidates):
            current = candidate_offset + offset + 1
            if should_stop is not None and should_stop():
                break

            def candidate_progress(
                progress: SearchProgress,
                *,
                _current: int = current,
            ) -> None:
                if progress_callback is None:
                    return
                progress_callback(
                    SearchProgress(
                        current_candidate=_current,
                        total_candidates=total,
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        phase=progress.phase,
                        window_index=progress.window_index,
                        total_windows=progress.total_windows,
                    )
                )

            result = evaluate_candidate(
                candidate,
                self.config,
                self.backtest_runner,
                ohlcv=ohlcv,
                progress_callback=candidate_progress,
                should_stop=should_stop,
            )
            results.append(result)

            if progress_callback is not None:
                progress_callback(
                    SearchProgress(
                        current_candidate=current,
                        total_candidates=total,
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        phase="done",
                        window_index=None,
                        total_windows=None,
                    )
                )

        provider.report(results)
        ranked = _rank_results(results)
        best = ranked[0] if ranked and ranked[0].rank == 1 else None
        metadata: dict[str, dict[str, Any]] | None = None
        if hasattr(provider, "candidate_metadata"):
            provider_metadata = provider.candidate_metadata()
            if provider_metadata:
                metadata = provider_metadata
        return StrategySearchResult(
            candidates=ranked,
            objective_mode=self.config.objective.mode,
            best=best,
            candidate_metadata=metadata,
        )
