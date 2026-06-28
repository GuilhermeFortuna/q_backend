"""Genetic strategy search — provider, orchestrator, and job selection seam."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Any

import pandas as pd

from q_backend.backtesting.genome.activity import genome_signal_activity
from q_backend.backtesting.genome.exit_rule_policy import exit_policy_metadata_for_genome
from q_backend.backtesting.genome.operators import (
    DEFAULT_MUTATION_OPERATOR_WEIGHTS,
    adapt_mutation_rate,
    build_initial_population,
    clone_genome,
    crossover_genomes,
    draw_valid_child,
    genome_node_count,
    genome_param_count,
    mutate_genome,
    population_structural_diversity,
    update_mutation_operator_weights,
)
from q_backend.backtesting.genome.schema import Genome
from q_backend.storage.db.engine import session_scope
from q_backend.backtesting.genome.latent_universe import (
    LatentUniverse,
    empty_latent_universe,
    resolve_latent_universe,
)
from q_backend.backtesting.genome.score_bias import build_kind_weights
from q_backend.backtesting.genome.search_space import derive_genome_search_space
from q_backend.optimization.auto_search_space import default_risk_search_space
from q_backend.optimization.backtest_runner import BacktestRunConfig, BacktestRunner, DefaultBacktestRunner
from q_backend.optimization.dsr import deflated_sharpe_ratio
from q_backend.optimization.lockbox import (
    backtest_config_for_walkforward,
    evaluate_lockbox,
)
from q_backend.optimization.genetic_parallel import (
    evaluate_generation_parallel,
    eval_config_for_parallel_workers,
)
from q_backend.optimization.models import SearchSpaceConfig
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    GateConfig,
    GeneticFinalizeSummary,
    GeneticSearchConfig,
    SearchCandidate,
    SearchProgress,
    StrategySearchConfig,
    StrategySearchResult,
    StrategySearchRunner,
    _rank_results,
    evaluate_candidate,
)

_NO_RESULT_PROGRESS_BONUS = 0.5
_EFFICIENCY_LOG_CAP = 2.0


def _complexity_penalty(genome: Genome, genetic: GeneticSearchConfig) -> float:
    return (
        genetic.complexity_lambda * genome_node_count(genome)
        + genetic.complexity_mu * genome_param_count(genome)
    )


def _efficiency_gate_penalty(
    efficiency: float | None,
    gates: GateConfig,
    genetic: GeneticSearchConfig,
) -> float:
    target = (gates.efficiency_low + gates.efficiency_high) / 2.0
    if efficiency is None or target <= 0:
        return genetic.gate_penalty_efficiency
    ratio = max(efficiency, 1e-6) / target
    return genetic.gate_penalty_efficiency * min(
        abs(math.log(ratio)),
        _EFFICIENCY_LOG_CAP,
    )


def _soft_gate_penalty(
    result: CandidateResult,
    gates: GateConfig,
    genetic: GeneticSearchConfig,
) -> tuple[float, dict[str, float]]:
    breakdown: dict[str, float] = {}
    total = 0.0

    if "few_oos_trades" in result.gate_flags:
        oos_trades = int((result.oos_metrics or {}).get("total_trades", 0))
        if gates.min_oos_trades > 0:
            shortfall = (gates.min_oos_trades - oos_trades) / gates.min_oos_trades
            ratio = max(0.0, min(1.0, shortfall))
        else:
            ratio = 0.0
        penalty = genetic.gate_penalty_trades * ratio
        breakdown["few_oos_trades"] = penalty
        total += penalty

    if "few_windows" in result.gate_flags:
        missing = max(0, gates.min_completed_windows - result.completed_windows)
        if gates.min_completed_windows > 0:
            ratio = missing / gates.min_completed_windows
        else:
            ratio = 0.0
        penalty = genetic.gate_penalty_windows * ratio
        breakdown["few_windows"] = penalty
        total += penalty

    for flag in ("low_efficiency", "suspicious_efficiency"):
        if flag in result.gate_flags:
            penalty = _efficiency_gate_penalty(result.efficiency, gates, genetic)
            breakdown[flag] = penalty
            total += penalty

    return total, breakdown


def candidate_fitness_details(
    result: CandidateResult,
    genome: Genome,
    genetic: GeneticSearchConfig,
    gates: GateConfig | None = None,
) -> tuple[float, dict[str, Any]]:
    """Graded selection fitness and a soft-penalty breakdown for metadata."""
    gates = gates or GateConfig()
    complexity_penalty = _complexity_penalty(genome, genetic)
    breakdown: dict[str, Any] = {"complexity_penalty": complexity_penalty}

    if result.status in {"error", "unsupported"}:
        breakdown["band"] = "error"
        return genetic.error_floor, breakdown

    oos_trades = int((result.oos_metrics or {}).get("total_trades", 0))
    if result.status == "no_result" or (
        result.status == "completed" and oos_trades == 0
    ):
        progress = result.completed_windows / max(result.window_count, 1)
        progress_bonus = _NO_RESULT_PROGRESS_BONUS * progress
        breakdown["band"] = "no_result"
        breakdown["progress_bonus"] = progress_bonus
        return genetic.no_result_floor + progress_bonus, breakdown

    if result.status != "completed" or result.robustness_score is None:
        breakdown["band"] = "error"
        return genetic.error_floor, breakdown

    base = result.robustness_score - complexity_penalty
    breakdown["robustness_score"] = result.robustness_score
    breakdown["band"] = "passed" if result.passed_gates else "failed_gates"

    if result.passed_gates:
        return base, breakdown

    gate_penalty, gate_breakdown = _soft_gate_penalty(result, gates, genetic)
    breakdown["gate_penalties"] = gate_breakdown
    breakdown["gate_penalty_total"] = gate_penalty
    return base - gate_penalty, breakdown


def candidate_fitness(
    result: CandidateResult,
    genome: Genome,
    genetic: GeneticSearchConfig,
    gates: GateConfig | None = None,
) -> float:
    fitness, _breakdown = candidate_fitness_details(result, genome, genetic, gates)
    return fitness


def _resolve_probe_frame(
    backtest_runner: BacktestRunner,
    config: StrategySearchConfig,
) -> pd.DataFrame | None:
    """Reuse the runner's cached frame or one slice from its data provider."""
    df = getattr(backtest_runner, "_df", None)
    if df is not None and len(df) > 0:
        return df
    if not isinstance(backtest_runner, DefaultBacktestRunner):
        return None
    provider = getattr(backtest_runner, "_data_provider", None)
    if provider is None:
        return None
    backtest = config.backtest
    run_cfg = BacktestRunConfig(
        symbol=backtest.symbol,
        timeframe=backtest.timeframe,
        start=backtest.start,
        end=backtest.end,
        initial_capital=backtest.initial_capital,
        point_value=backtest.point_value,
        strategy=backtest.strategy or "CompositeStrategy",
        strategy_params={},
        position_sizing=None,
    )
    try:
        resolved = provider(run_cfg)
    except Exception:
        return None
    if resolved is None or len(resolved) == 0:
        return None
    return resolved


def _search_space_for_genome(
    genome: Genome,
    search_config: StrategySearchConfig,
) -> SearchSpaceConfig:
    search_space, _ = derive_genome_search_space(genome)
    if not search_config.include_risk_search:
        return search_space
    risk_space = default_risk_search_space()
    return SearchSpaceConfig(
        strategy_params=search_space.strategy_params,
        risk_params=risk_space.risk_params,
    )


def search_candidate_for_genome(
    genome: Genome,
    search_config: StrategySearchConfig,
    *,
    latent_model_hash: str | None = None,
) -> SearchCandidate:
    """Build the WO31 ``SearchCandidate`` for a single genome.

    Shared by ``GeneticCandidateProvider.candidates`` (in-process orchestrator) and
    the distributed per-generation candidate worker, so both construct an identical
    candidate from a genome.
    """
    fixed_params: dict[str, Any] = {"genome": genome.model_dump()}
    if latent_model_hash is not None:
        fixed_params["latent_model_hash"] = latent_model_hash
    fixed_params["timeframe"] = search_config.backtest.timeframe
    return SearchCandidate(
        candidate_id=genome.genome_id,
        strategy="CompositeStrategy",
        search_space=_search_space_for_genome(genome, search_config),
        fixed_params=fixed_params,
    )


def create_genetic_candidate_provider(
    genetic_config: GeneticSearchConfig,
    search_config: StrategySearchConfig,
    probe_df: pd.DataFrame | None = None,
    latents_enabled: bool = True,
) -> GeneticCandidateProvider:
    """Build a genetic provider after resolving the per-run latent universe once."""
    backtest = search_config.backtest
    if not latents_enabled:
        latent_universe = empty_latent_universe()
        kind_weights: dict[str, float] = {}
    else:
        with session_scope() as session:
            latent_universe = resolve_latent_universe(
                session,
                backtest.symbol,
                backtest.timeframe,
            )
            kind_weights = build_kind_weights(
                session,
                symbol=backtest.symbol,
                timeframe=backtest.timeframe,
                n_latents=latent_universe.n_latents,
                start=backtest.start,
                end=backtest.end,
                latent_model_hash=latent_universe.latent_model_hash,
            )
    return GeneticCandidateProvider(
        genetic_config,
        search_config,
        probe_df=probe_df,
        latent_universe=latent_universe,
        kind_weights=kind_weights,
    )


class GeneticCandidateProvider:
    """Evolve a population of genomes via WO31's ``CandidateProvider`` seam."""

    def __init__(
        self,
        genetic_config: GeneticSearchConfig,
        search_config: StrategySearchConfig,
        probe_df: pd.DataFrame | None = None,
        latent_universe: LatentUniverse | None = None,
        kind_weights: dict[str, float] | None = None,
    ) -> None:
        self._genetic = genetic_config
        self._search = search_config
        self._probe_df = probe_df
        self._latent_universe = latent_universe or empty_latent_universe()
        self._kind_weights = kind_weights or {}
        self._rng = random.Random(genetic_config.init_seed)
        self._generation = 0
        self._next_individual = 0
        self._population: list[Genome] = build_initial_population(
            self._rng,
            population_size=genetic_config.population_size,
            max_nodes=genetic_config.max_nodes,
            max_depth=genetic_config.max_depth,
            ohlcv=probe_df,
            min_seed_signals=genetic_config.min_seed_signals,
            repair_max_attempts=genetic_config.repair_max_attempts,
            seed_exit_policies=genetic_config.seed_exit_policies,
            exit_policy_preset_ids=genetic_config.exit_policy_preset_ids,
            exit_policy_seed_fraction=genetic_config.exit_policy_seed_fraction,
            indicator_kinds=self._latent_universe.indicator_kinds,
            n_latents=self._latent_universe.n_latents,
            kind_weights=self._kind_weights,
        )
        self._genome_by_id = {genome.genome_id: genome for genome in self._population}
        self._champion: Genome | None = None
        self._champion_fitness = float("-inf")
        self._best_fitness = float("-inf")
        self._stagnation_generations = 0
        self._effective_mutation_rate = genetic_config.mutation_rate
        self._mutation_operator_weights = dict(DEFAULT_MUTATION_OPERATOR_WEIGHTS)
        self._parent_fitness_by_id: dict[str, float] = {}
        self.initial_population = [clone_genome(genome) for genome in self._population]

    @property
    def effective_mutation_rate(self) -> float:
        return self._effective_mutation_rate

    @property
    def stagnation_generations(self) -> int:
        return self._stagnation_generations

    @property
    def structural_diversity(self) -> float:
        return population_structural_diversity(self._population)

    @property
    def mutation_operator_weights(self) -> dict[str, float]:
        return dict(self._mutation_operator_weights)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def population(self) -> list[Genome]:
        return list(self._population)

    def candidates(self) -> list[SearchCandidate]:
        return [
            search_candidate_for_genome(
                genome,
                self._search,
                latent_model_hash=self._latent_universe.latent_model_hash,
            )
            for genome in self._population
        ]

    def export_state(self) -> dict[str, Any]:
        """Serialize the evolving state so a later worker can resume breeding.

        The distributed flow evaluates each generation in separate worker processes,
        so the RNG/champion/population state must round-trip through Redis between
        generations. JSON-safe: ``random.Random`` state is a tuple of ints, and
        ``best_fitness`` may be ``-inf`` (json dumps/loads it as ``-Infinity``).
        """
        rng_version, rng_internal, rng_gauss = self._rng.getstate()
        return {
            "rng": [rng_version, list(rng_internal), rng_gauss],
            "generation": self._generation,
            "next_individual": self._next_individual,
            "best_fitness": self._best_fitness,
            "stagnation_generations": self._stagnation_generations,
            "effective_mutation_rate": self._effective_mutation_rate,
            "mutation_operator_weights": self._mutation_operator_weights,
            "parent_fitness_by_id": self._parent_fitness_by_id,
            "champion": self._champion.model_dump() if self._champion else None,
            "population": [genome.model_dump() for genome in self._population],
        }

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore state produced by ``export_state`` (in a fresh worker process)."""
        rng_version, rng_internal, rng_gauss = state["rng"]
        self._rng.setstate((rng_version, tuple(rng_internal), rng_gauss))
        self._generation = state["generation"]
        self._next_individual = state["next_individual"]
        self._best_fitness = state["best_fitness"]
        self._stagnation_generations = state.get("stagnation_generations", 0)
        self._effective_mutation_rate = state.get(
            "effective_mutation_rate",
            self._genetic.mutation_rate,
        )
        self._mutation_operator_weights = state.get(
            "mutation_operator_weights",
            dict(DEFAULT_MUTATION_OPERATOR_WEIGHTS),
        )
        self._parent_fitness_by_id = state.get("parent_fitness_by_id", {})
        champion = state.get("champion")
        self._champion = Genome.model_validate(champion) if champion else None
        self._population = [Genome.model_validate(g) for g in state["population"]]
        self._genome_by_id = {
            genome.genome_id: genome for genome in self._population
        }

    def report(self, results: list[CandidateResult]) -> None:
        scored: list[tuple[float, Genome, CandidateResult]] = []
        generation_best = self._best_fitness
        for result in results:
            genome = self._genome_by_id.get(result.candidate_id)
            if genome is None:
                continue
            fitness = candidate_fitness(
                result,
                genome,
                self._genetic,
                gates=self._search.gates,
            )
            scored.append((fitness, genome, result))
            if fitness > self._best_fitness:
                self._best_fitness = fitness
            if (
                result.status == "completed"
                and result.passed_gates
                and fitness > self._champion_fitness
            ):
                self._champion_fitness = fitness
                self._champion = clone_genome(genome)

            if self._genetic.adaptive_operator_weights:
                mutation_op = genome.metadata.get("last_mutation_op")
                parent_ids = genome.metadata.get("parent_ids") or []
                if mutation_op and parent_ids:
                    parent_fitnesses = [
                        self._parent_fitness_by_id.get(parent_id)
                        for parent_id in parent_ids
                    ]
                    parent_fitnesses = [
                        value for value in parent_fitnesses if value is not None
                    ]
                    if parent_fitnesses:
                        parent_best = max(parent_fitnesses)
                        self._mutation_operator_weights = update_mutation_operator_weights(
                            self._mutation_operator_weights,
                            operator=str(mutation_op),
                            fitness_delta=fitness - parent_best,
                        )

        if self._best_fitness > generation_best:
            self._stagnation_generations = 0
        else:
            self._stagnation_generations += 1

        structural_diversity = population_structural_diversity(self._population)
        self._effective_mutation_rate = adapt_mutation_rate(
            base_rate=self._genetic.mutation_rate,
            min_rate=self._genetic.mutation_rate_min,
            max_rate=self._genetic.mutation_rate_max,
            stagnation_generations=self._stagnation_generations,
            stagnation_patience=self._genetic.stagnation_patience,
            structural_diversity=structural_diversity,
        )

        scored.sort(key=lambda item: item[0], reverse=True)
        reproducers = [(fitness, genome) for fitness, genome, _result in scored]
        self._parent_fitness_by_id = {
            genome.genome_id: fitness for fitness, genome in reproducers
        }

        next_population: list[Genome] = []
        elite_ids: set[str] = set()
        for _fitness, genome in reproducers[: self._genetic.elite_count]:
            elite = clone_genome(
                genome,
                genome_id=self._new_genome_id(),
                generation=self._generation + 1,
                parent_ids=[genome.genome_id],
            )
            next_population.append(elite)
            elite_ids.add(genome.genome_id)

        while len(next_population) < self._genetic.population_size:
            if len(reproducers) < 2:
                parent = self._rng.choice(self._population)
                child = self._vary(parent, parent)
            else:
                parent_a = self._tournament_select(reproducers)
                parent_b = self._tournament_select(reproducers)
                child = self._vary(parent_a, parent_b)
            next_population.append(child)

        self._generation += 1
        self._population = next_population[: self._genetic.population_size]
        self._genome_by_id = {genome.genome_id: genome for genome in self._population}

    def champion(self) -> Genome | None:
        return self._champion

    def _new_genome_id(self) -> str:
        genome_id = f"gen{self._generation}-ind{self._next_individual}"
        self._next_individual += 1
        return genome_id

    def _tournament_select(
        self,
        reproducers: list[tuple[float, Genome]],
    ) -> Genome:
        tournament = self._rng.sample(
            reproducers,
            k=min(self._genetic.tournament_size, len(reproducers)),
        )
        tournament.sort(key=lambda item: item[0], reverse=True)
        return tournament[0][1]

    def _vary(self, parent_a: Genome, parent_b: Genome) -> Genome:
        generation = self._generation + 1
        parent_ids = sorted({parent_a.genome_id, parent_b.genome_id})

        def build_child() -> Genome:
            if self._rng.random() < self._genetic.crossover_rate:
                child = crossover_genomes(
                    self._rng,
                    parent_a,
                    parent_b,
                    max_nodes=self._genetic.max_nodes,
                    max_depth=self._genetic.max_depth,
                )
            else:
                child = clone_genome(parent_a)
            child_metadata = dict(child.metadata)
            child_metadata.pop("last_mutation_op", None)
            child.metadata = child_metadata
            if self._rng.random() < self._effective_mutation_rate:
                operator_weights = (
                    self._mutation_operator_weights
                    if self._genetic.adaptive_operator_weights
                    else None
                )
                child = mutate_genome(
                    self._rng,
                    child,
                    max_nodes=self._genetic.max_nodes,
                    max_depth=self._genetic.max_depth,
                    probe_df=self._probe_df,
                    min_signals=self._genetic.min_seed_signals,
                    repair_max_attempts=self._genetic.repair_max_attempts,
                    operator_weights=operator_weights,
                    exit_policy_preset_ids=self._genetic.exit_policy_preset_ids,
                    indicator_kinds=self._latent_universe.indicator_kinds,
                    n_latents=self._latent_universe.n_latents,
                )
            return clone_genome(
                child,
                genome_id=self._new_genome_id(),
                generation=generation,
                parent_ids=parent_ids,
            )

        return draw_valid_child(self._rng, build_child)


def _config_for_walkforward(config: StrategySearchConfig) -> StrategySearchConfig:
    if not config.lockbox.enabled:
        return config
    backtest = backtest_config_for_walkforward(config.backtest, config.lockbox)
    return config.model_copy(update={"backtest": backtest})


def _daily_returns_from_equity(equity: pd.Series | None) -> list[float]:
    if equity is None or len(equity) < 2:
        return []
    daily = equity.resample("D").last().ffill()
    returns = daily.pct_change().dropna()
    return [float(value) for value in returns.tolist()]


class GeneticStrategySearchOrchestrator:
    """Run G generations by reusing ``evaluate_candidate`` unchanged."""

    def __init__(
        self,
        config: StrategySearchConfig,
        provider: GeneticCandidateProvider,
        backtest_runner: BacktestRunner,
    ) -> None:
        if config.genetic is None:
            raise ValueError("StrategySearchConfig.genetic is required for genetic search")
        self.config = config
        self.provider = provider
        self.backtest_runner = backtest_runner
        self._generations: list[list[CandidateResult]] = []
        self._candidate_metadata: dict[str, dict[str, Any]] = {}
        self._eval_config = _config_for_walkforward(config)
        self._probe_frame = _resolve_probe_frame(backtest_runner, config)

    @property
    def generations(self) -> list[list[CandidateResult]]:
        return self._generations

    @property
    def candidate_metadata(self) -> dict[str, dict[str, Any]]:
        return dict(self._candidate_metadata)

    def _prescreen_result(
        self,
        candidate: SearchCandidate,
        genome: Genome | None,
        *,
        genetic: GeneticSearchConfig,
        probe_frame: pd.DataFrame | None,
    ) -> CandidateResult | None:
        prescreen_min = genetic.prescreen_min_signals
        if (
            prescreen_min <= 0
            or probe_frame is None
            or len(probe_frame) == 0
            or genome is None
            or genome_signal_activity(
                genome,
                probe_frame,
                min_signals=prescreen_min,
                max_depth=genetic.max_depth,
                max_node_count=genetic.max_nodes,
            ).is_tradeable
        ):
            return None
        return CandidateResult(
            candidate_id=candidate.candidate_id,
            strategy=candidate.strategy,
            status="no_result",
            error="pre-screen: no in-sample signals",
        )

    def _record_candidate_metadata(
        self,
        result: CandidateResult,
        genome: Genome | None,
        *,
        genetic: GeneticSearchConfig,
    ) -> None:
        if genome is None:
            return
        selection_fitness, fitness_breakdown = candidate_fitness_details(
            result,
            genome,
            genetic,
            gates=self.config.gates,
        )
        self._candidate_metadata[result.candidate_id] = {
            "generation": self.provider.generation,
            "genome": genome.model_dump(),
            "genome_node_count": genome_node_count(genome),
            "complexity_penalty": _complexity_penalty(genome, genetic),
            "selection_fitness": selection_fitness,
            "fitness_breakdown": fitness_breakdown,
        }
        policy_meta = exit_policy_metadata_for_genome(genome)
        if policy_meta:
            self._candidate_metadata[result.candidate_id].update(policy_meta)

    def _evaluate_generation_serial(
        self,
        candidates: list[SearchCandidate],
        *,
        genetic: GeneticSearchConfig,
        ohlcv: pd.DataFrame | None,
        generation_number: int,
        progress_callback: Callable[[SearchProgress], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> list[CandidateResult]:
        generation_results: list[CandidateResult] = []
        for candidate_index, candidate in enumerate(candidates):
            if should_stop is not None and should_stop():
                break

            def candidate_progress(
                progress: SearchProgress,
                *,
                _candidate_index: int = candidate_index,
                _generation: int = self.provider.generation,
            ) -> None:
                if progress_callback is None:
                    return
                progress_callback(
                    SearchProgress(
                        current_candidate=_candidate_index + 1,
                        total_candidates=len(candidates),
                        candidate_id=progress.candidate_id,
                        strategy=progress.strategy,
                        phase=progress.phase,
                        window_index=progress.window_index,
                        total_windows=progress.total_windows,
                        generation=_generation + 1,
                        total_generations=genetic.generations,
                    )
                )

            genome = self.provider._genome_by_id.get(candidate.candidate_id)
            prescreened = self._prescreen_result(
                candidate,
                genome,
                genetic=genetic,
                probe_frame=self._probe_frame,
            )
            if prescreened is not None:
                result = prescreened
            else:
                result = evaluate_candidate(
                    candidate,
                    self._eval_config,
                    self.backtest_runner,
                    ohlcv=ohlcv,
                    progress_callback=candidate_progress,
                    should_stop=should_stop,
                )

            generation_results.append(result)
            self._record_candidate_metadata(result, genome, genetic=genetic)

            if progress_callback is not None:
                progress_callback(
                    SearchProgress(
                        current_candidate=candidate_index + 1,
                        total_candidates=len(candidates),
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        phase="done",
                        window_index=None,
                        total_windows=None,
                        generation=generation_number,
                        total_generations=genetic.generations,
                    )
                )

        return generation_results

    def _evaluate_generation(
        self,
        candidates: list[SearchCandidate],
        *,
        genetic: GeneticSearchConfig,
        ohlcv: pd.DataFrame | None,
        eval_frame: pd.DataFrame | None,
        generation_number: int,
        progress_callback: Callable[[SearchProgress], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> list[CandidateResult]:
        workers = resolve_worker_count(genetic.max_workers, len(candidates))
        if (
            workers == 1
            or eval_frame is None
            or len(eval_frame) == 0
        ):
            return self._evaluate_generation_serial(
                candidates,
                genetic=genetic,
                ohlcv=ohlcv,
                generation_number=generation_number,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

        entries: list[tuple[SearchCandidate, CandidateResult | None]] = []
        for candidate in candidates:
            if should_stop is not None and should_stop():
                break
            genome = self.provider._genome_by_id.get(candidate.candidate_id)
            prescreened = self._prescreen_result(
                candidate,
                genome,
                genetic=genetic,
                probe_frame=self._probe_frame,
            )
            entries.append((candidate, prescreened))

        eval_candidates = [candidate for candidate, result in entries if result is None]
        evaluated: list[CandidateResult] = []
        if eval_candidates:
            evaluated = evaluate_generation_parallel(
                eval_candidates,
                eval_config_for_parallel_workers(self._eval_config),
                eval_frame,
                max_workers=genetic.max_workers,
                generation=generation_number,
                total_generations=genetic.generations,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

        generation_results: list[CandidateResult] = []
        eval_iter = iter(evaluated)
        for candidate, existing in entries:
            if should_stop is not None and should_stop():
                break
            genome = self.provider._genome_by_id.get(candidate.candidate_id)
            if existing is not None:
                result = existing
            else:
                try:
                    result = next(eval_iter)
                except StopIteration:
                    break
            generation_results.append(result)
            self._record_candidate_metadata(result, genome, genetic=genetic)
            if existing is not None and progress_callback is not None:
                progress_callback(
                    SearchProgress(
                        current_candidate=len(generation_results),
                        total_candidates=len(candidates),
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        phase="done",
                        window_index=None,
                        total_windows=None,
                        generation=generation_number,
                        total_generations=genetic.generations,
                    )
                )

        return generation_results

    def run(
        self,
        progress_callback: Callable[[SearchProgress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> StrategySearchResult:
        genetic = self.config.genetic
        assert genetic is not None
        ohlcv = getattr(self.backtest_runner, "_df", None)
        eval_frame = (
            self._probe_frame
            if self._probe_frame is not None
            else ohlcv
        )

        for _generation_index in range(genetic.generations):
            if should_stop is not None and should_stop():
                break

            candidates = self.provider.candidates()
            generation_number = self.provider.generation + 1
            generation_results = self._evaluate_generation(
                candidates,
                genetic=genetic,
                ohlcv=ohlcv,
                eval_frame=eval_frame,
                generation_number=generation_number,
                progress_callback=progress_callback,
                should_stop=should_stop,
            )

            self._generations.append(generation_results)
            self.provider.report(generation_results)

            if should_stop is not None and should_stop():
                break

        return self.finalize()

    def finalize(self) -> StrategySearchResult:
        """Post-rank DSR + lock-box evaluation; does not alter per-candidate scores."""
        if not self._generations:
            return StrategySearchResult(
                candidates=[],
                objective_mode=self.config.objective.mode,
                best=None,
                genetic_summary=GeneticFinalizeSummary(),
                all_generations=[],
                candidate_metadata={},
            )

        final_results = self._generations[-1]
        ranked = _rank_results(final_results)
        best = ranked[0] if ranked and ranked[0].rank == 1 else None

        genetic = self.config.genetic
        assert genetic is not None
        total_genomes = sum(len(generation) for generation in self._generations)
        generations_completed = len(self._generations)

        summary = GeneticFinalizeSummary(
            generations_completed=generations_completed,
            total_genomes_evaluated=total_genomes,
            n_trials_effective=total_genomes,
        )

        champion_dsr: float | None = None
        if best is not None and best.oos_metrics is not None:
            sr_observed = float(best.oos_metrics.get("sharpe_ratio", 0.0))
            summary.sr_observed = sr_observed
            returns = _daily_returns_from_equity(best.oos_equity_curve)
            num_obs = max(len(returns), 2)
            skewness = 0.0
            kurtosis = 3.0
            if len(returns) >= 2:
                series = pd.Series(returns)
                skewness = float(series.skew())
                kurtosis = float(series.kurtosis())
            champion_dsr = deflated_sharpe_ratio(
                sr_observed=sr_observed,
                num_trials=total_genomes,
                num_observations=num_obs,
                skewness=skewness,
                kurtosis=kurtosis,
            )
            summary.champion_dsr = champion_dsr
            if best.candidate_id in self._candidate_metadata:
                self._candidate_metadata[best.candidate_id]["dsr"] = champion_dsr

        if best is not None and best.best_params is not None:
            lockbox_params = dict(best.best_params)
            champion_meta = self._candidate_metadata.get(best.candidate_id, {})
            genome = champion_meta.get("genome")
            if genome is not None:
                strategy_params = dict(lockbox_params.get("strategy_params", {}))
                strategy_params["genome"] = genome
                lockbox_params["strategy_params"] = strategy_params
            lockbox_metrics, lockbox_passed, lockbox_equity = evaluate_lockbox(
                backtest=self.config.backtest,
                lockbox=self.config.lockbox,
                best_params=lockbox_params,
                strategy=best.strategy,
                backtest_runner=self.backtest_runner,
            )
            summary.lockbox_metrics = lockbox_metrics
            summary.lockbox_passed = lockbox_passed
            summary.lockbox_equity_curve = lockbox_equity

        return StrategySearchResult(
            candidates=ranked,
            objective_mode=self.config.objective.mode,
            best=best,
            genetic_summary=summary,
            all_generations=list(self._generations),
            candidate_metadata=dict(self._candidate_metadata),
        )

    def _assemble_result(self) -> StrategySearchResult:
        return self.finalize()


def select_search_orchestrator(
    config: StrategySearchConfig,
    backtest_runner: BacktestRunner,
    provider: CandidateProvider | None = None,
) -> StrategySearchRunner | GeneticStrategySearchOrchestrator:
    """Return the genetic orchestrator when ``config.genetic`` is set, else WO31 runner."""
    if config.genetic is not None:
        genetic_provider = provider
        if genetic_provider is None:
            genetic_provider = create_genetic_candidate_provider(
                config.genetic,
                config,
                probe_df=_resolve_probe_frame(backtest_runner, config),
                latents_enabled=config.latents_enabled,
            )
        if not isinstance(genetic_provider, GeneticCandidateProvider):
            raise TypeError(
                "Genetic search requires GeneticCandidateProvider when config.genetic is set"
            )
        return GeneticStrategySearchOrchestrator(
            config,
            genetic_provider,
            backtest_runner,
        )
    return StrategySearchRunner(config, backtest_runner, provider=provider)
