"""Genetic strategy search — provider, orchestrator, and job selection seam."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from q_backend.backtesting.genome.operators import (
    build_initial_population,
    clone_genome,
    crossover_genomes,
    draw_valid_child,
    genome_node_count,
    genome_param_count,
    mutate_genome,
)
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.search_space import derive_genome_search_space
from q_backend.optimization.auto_search_space import default_risk_search_space
from q_backend.optimization.backtest_runner import BacktestRunner
from q_backend.optimization.models import SearchSpaceConfig
from q_backend.optimization.strategy_search import (
    CandidateProvider,
    CandidateResult,
    GeneticSearchConfig,
    SearchCandidate,
    SearchProgress,
    StrategySearchConfig,
    StrategySearchResult,
    StrategySearchRunner,
    _rank_results,
    evaluate_candidate,
)


def candidate_fitness(
    result: CandidateResult,
    genome: Genome,
    genetic: GeneticSearchConfig,
) -> float:
    if result.status != "completed" or not result.passed_gates:
        return float("-inf")
    if result.robustness_score is None:
        return float("-inf")
    penalty = (
        genetic.complexity_lambda * genome_node_count(genome)
        + genetic.complexity_mu * genome_param_count(genome)
    )
    return result.robustness_score - penalty


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


class GeneticCandidateProvider:
    """Evolve a population of genomes via WO31's ``CandidateProvider`` seam."""

    def __init__(
        self,
        genetic_config: GeneticSearchConfig,
        search_config: StrategySearchConfig,
    ) -> None:
        self._genetic = genetic_config
        self._search = search_config
        self._rng = random.Random(genetic_config.init_seed)
        self._generation = 0
        self._next_individual = 0
        self._population: list[Genome] = build_initial_population(
            self._rng,
            population_size=genetic_config.population_size,
            max_nodes=genetic_config.max_nodes,
            max_depth=genetic_config.max_depth,
        )
        self._genome_by_id = {genome.genome_id: genome for genome in self._population}
        self._champion: Genome | None = None
        self._best_fitness = float("-inf")
        self.initial_population = [clone_genome(genome) for genome in self._population]

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def population(self) -> list[Genome]:
        return list(self._population)

    def candidates(self) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        for genome in self._population:
            genome_dict = genome.model_dump()
            candidates.append(
                SearchCandidate(
                    candidate_id=genome.genome_id,
                    strategy="CompositeStrategy",
                    search_space=_search_space_for_genome(genome, self._search),
                    fixed_params={"genome": genome_dict},
                )
            )
        return candidates

    def report(self, results: list[CandidateResult]) -> None:
        scored: list[tuple[float, Genome, CandidateResult]] = []
        for result in results:
            genome = self._genome_by_id.get(result.candidate_id)
            if genome is None:
                continue
            fitness = candidate_fitness(result, genome, self._genetic)
            scored.append((fitness, genome, result))
            if fitness > self._best_fitness:
                self._best_fitness = fitness
                self._champion = clone_genome(genome)

        scored.sort(key=lambda item: item[0], reverse=True)
        reproducers = [
            (fitness, genome)
            for fitness, genome, _result in scored
            if fitness > float("-inf")
        ]
        if not reproducers:
            reproducers = [(float("-inf"), genome) for genome in self._population]

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
            if self._rng.random() < self._genetic.mutation_rate:
                child = mutate_genome(
                    self._rng,
                    child,
                    max_nodes=self._genetic.max_nodes,
                    max_depth=self._genetic.max_depth,
                )
            return clone_genome(
                child,
                genome_id=self._new_genome_id(),
                generation=generation,
                parent_ids=parent_ids,
            )

        return draw_valid_child(self._rng, build_child)


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

    @property
    def generations(self) -> list[list[CandidateResult]]:
        return self._generations

    def run(
        self,
        progress_callback: Callable[[SearchProgress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> StrategySearchResult:
        genetic = self.config.genetic
        assert genetic is not None
        ohlcv = getattr(self.backtest_runner, "_df", None)

        for _generation_index in range(genetic.generations):
            if should_stop is not None and should_stop():
                break

            candidates = self.provider.candidates()
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

                result = evaluate_candidate(
                    candidate,
                    self.config,
                    self.backtest_runner,
                    ohlcv=ohlcv,
                    progress_callback=candidate_progress,
                    should_stop=should_stop,
                )
                generation_results.append(result)

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
                            generation=self.provider.generation + 1,
                            total_generations=genetic.generations,
                        )
                    )

            self._generations.append(generation_results)
            self.provider.report(generation_results)

            if should_stop is not None and should_stop():
                break

        return self._assemble_result()

    def _assemble_result(self) -> StrategySearchResult:
        if not self._generations:
            return StrategySearchResult(
                candidates=[],
                objective_mode=self.config.objective.mode,
                best=None,
            )
        final_results = self._generations[-1]
        ranked = _rank_results(final_results)
        best = ranked[0] if ranked and ranked[0].rank == 1 else None
        return StrategySearchResult(
            candidates=ranked,
            objective_mode=self.config.objective.mode,
            best=best,
        )


def select_search_orchestrator(
    config: StrategySearchConfig,
    backtest_runner: BacktestRunner,
    provider: CandidateProvider | None = None,
) -> StrategySearchRunner | GeneticStrategySearchOrchestrator:
    """Return the genetic orchestrator when ``config.genetic`` is set, else WO31 runner."""
    if config.genetic is not None:
        genetic_provider = provider
        if genetic_provider is None:
            genetic_provider = GeneticCandidateProvider(config.genetic, config)
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
