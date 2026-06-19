"""Tests for graded genetic selection fitness (WO52)."""

from __future__ import annotations

import math

import pytest

from q_backend.backtesting.genome.operators import clone_genome
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome
from q_backend.optimization.genetic_search import (
    GeneticCandidateProvider,
    candidate_fitness,
    candidate_fitness_details,
)
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GateConfig,
    GeneticSearchConfig,
    StrategySearchConfig,
)
from q_backend.optimization.walkforward import WalkForwardConfig


def _search_config() -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study={"name": "fitness", "n_trials": 1},
    )


def _genome() -> Genome:
    return Genome.model_validate(REGISTRY_GENOME_FIXTURES["DonchianBreakout"])


def _completed(
    candidate_id: str,
    *,
    robustness_score: float,
    passed_gates: bool,
    gate_flags: list[str] | None = None,
    oos_trades: int = 20,
    efficiency: float = 1.0,
    completed_windows: int = 5,
    window_count: int = 5,
) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        strategy="CompositeStrategy",
        status="completed",
        passed_gates=passed_gates,
        gate_flags=gate_flags or [],
        objective_value=robustness_score,
        robustness_score=robustness_score,
        efficiency=efficiency,
        oos_metrics={"total_trades": oos_trades},
        completed_windows=completed_windows,
        window_count=window_count,
    )


def test_gradient_without_passers_returns_finite_ordered_values():
    genetic = GeneticSearchConfig()
    gates = GateConfig(min_oos_trades=10, min_completed_windows=2)
    genome = _genome()

    eight_trades = _completed(
        "eight",
        robustness_score=5.0,
        passed_gates=False,
        gate_flags=["few_oos_trades"],
        oos_trades=8,
    )
    one_trade = _completed(
        "one",
        robustness_score=5.0,
        passed_gates=False,
        gate_flags=["few_oos_trades"],
        oos_trades=1,
    )
    no_result = CandidateResult(
        candidate_id="nr",
        strategy="CompositeStrategy",
        status="no_result",
        completed_windows=2,
        window_count=5,
    )
    error = CandidateResult(
        candidate_id="err",
        strategy="CompositeStrategy",
        status="error",
        error="boom",
    )

    fitness_values = [
        candidate_fitness(eight_trades, genome, genetic, gates),
        candidate_fitness(one_trade, genome, genetic, gates),
        candidate_fitness(no_result, genome, genetic, gates),
        candidate_fitness(error, genome, genetic, gates),
    ]

    assert all(math.isfinite(value) for value in fitness_values)
    assert len(set(fitness_values)) == len(fitness_values)
    assert fitness_values[0] > fitness_values[1] > fitness_values[2] > fitness_values[3]


def test_top_band_preserved_for_passing_genomes():
    genetic = GeneticSearchConfig()
    genome = _genome()
    simple = Genome.model_validate(REGISTRY_GENOME_FIXTURES["DonchianBreakout"])
    complex_genome = Genome.model_validate(REGISTRY_GENOME_FIXTURES["MACrossover"])

    passing_a = _completed("a", robustness_score=10.0, passed_gates=True)
    passing_b = _completed("b", robustness_score=8.0, passed_gates=True)
    high_robustness_failing = _completed(
        "f",
        robustness_score=20.0,
        passed_gates=False,
        gate_flags=["few_oos_trades"],
        oos_trades=1,
    )

    assert candidate_fitness(passing_a, simple, genetic) > candidate_fitness(
        passing_b, simple, genetic
    )
    assert candidate_fitness(passing_a, simple, genetic) > candidate_fitness(
        passing_a, complex_genome, genetic
    )
    assert candidate_fitness(high_robustness_failing, simple, genetic) > candidate_fitness(
        passing_a, simple, genetic
    )


def test_champion_honesty_ignores_high_failing_graded_fitness():
    genetic = GeneticSearchConfig(population_size=10, elite_count=1, init_seed=1)
    provider = GeneticCandidateProvider(genetic, _search_config())
    population = provider.population

    failing = _completed(
        population[0].genome_id,
        robustness_score=100.0,
        passed_gates=False,
        gate_flags=["few_oos_trades"],
        oos_trades=1,
    )
    no_result = CandidateResult(
        candidate_id=population[1].genome_id,
        strategy="CompositeStrategy",
        status="no_result",
    )
    results = [failing, no_result] + [
        CandidateResult(
            candidate_id=genome.genome_id,
            strategy="CompositeStrategy",
            status="error",
            error="x",
        )
        for genome in population[2:]
    ]

    provider.report(results)
    assert provider.champion() is None

    passing_id = provider.population[0].genome_id
    passing = _completed(
        passing_id,
        robustness_score=1.0,
        passed_gates=True,
    )
    full_results = [
        passing if genome.genome_id == passing_id else CandidateResult(
            candidate_id=genome.genome_id,
            strategy="CompositeStrategy",
            status="error",
            error="x",
        )
        for genome in provider.population
    ]
    provider.report(full_results)
    champion = provider.champion()
    assert champion is not None
    assert champion.genome_id == passing.candidate_id


def test_selection_pressure_climbs_with_graded_fitness():
    genetic = GeneticSearchConfig(
        population_size=10,
        elite_count=1,
        init_seed=77,
        crossover_rate=0.0,
        mutation_rate=0.0,
    )
    provider = GeneticCandidateProvider(genetic, _search_config())
    mean_fitness: list[float] = []

    for generation in range(5):
        candidates = provider.candidates()
        results = []
        for index, candidate in enumerate(candidates):
            score = float(index + generation * 2)
            results.append(
                _completed(
                    candidate.candidate_id,
                    robustness_score=score,
                    passed_gates=False,
                    gate_flags=["few_oos_trades"],
                    oos_trades=max(1, index + generation),
                )
            )
        fitnesses = [
            candidate_fitness(result, provider.population[i], genetic)
            for i, result in enumerate(results)
        ]
        mean_fitness.append(sum(fitnesses) / len(fitnesses))
        provider.report(results)

    assert mean_fitness[-1] >= mean_fitness[0]
    assert any(
        later > earlier
        for earlier, later in zip(mean_fitness, mean_fitness[1:], strict=False)
    )

    def cliff_fitness(score: float) -> float:
        return float("-inf")

    cliff_means: list[float] = []
    for generation in range(5):
        scores = [float(index + generation * 2) for index in range(10)]
        cliff_means.append(
            sum(cliff_fitness(score) for score in scores) / len(scores)
        )
    assert cliff_means[-1] == cliff_means[0]


def test_determinism_same_seed_same_fitness_and_population_ids():
    genetic = GeneticSearchConfig(
        population_size=10,
        init_seed=2024,
        crossover_rate=0.0,
        mutation_rate=0.0,
    )
    search = _search_config()

    def run_once() -> tuple[list[list[float]], list[list[str]]]:
        provider = GeneticCandidateProvider(genetic, search)
        fitness_history: list[list[float]] = []
        id_history: list[list[str]] = [[g.genome_id for g in provider.population]]
        for generation in range(2):
            candidates = provider.candidates()
            results = [
                _completed(
                    candidate.candidate_id,
                    robustness_score=float(index + generation),
                    passed_gates=False,
                    gate_flags=["few_oos_trades"],
                    oos_trades=index + 1,
                )
                for index, candidate in enumerate(candidates)
            ]
            fitness_history.append(
                [
                    candidate_fitness(result, provider.population[i], genetic)
                    for i, result in enumerate(results)
                ]
            )
            provider.report(results)
            id_history.append([g.genome_id for g in provider.population])
        return fitness_history, id_history

    assert run_once() == run_once()


def test_fitness_details_include_breakdown_for_failed_gates():
    genetic = GeneticSearchConfig()
    gates = GateConfig()
    genome = _genome()
    result = _completed(
        "x",
        robustness_score=3.0,
        passed_gates=False,
        gate_flags=["few_oos_trades", "low_efficiency"],
        oos_trades=2,
        efficiency=0.1,
    )

    fitness, breakdown = candidate_fitness_details(result, genome, genetic, gates)

    assert math.isfinite(fitness)
    assert breakdown["band"] == "failed_gates"
    assert breakdown["gate_penalty_total"] > 0
    assert "few_oos_trades" in breakdown["gate_penalties"]


def test_candidate_fitness_contains_no_infinities():
    genetic = GeneticSearchConfig()
    genome = _genome()
    cases = [
        CandidateResult("a", "CompositeStrategy", status="error"),
        CandidateResult("b", "CompositeStrategy", status="unsupported"),
        CandidateResult("c", "CompositeStrategy", status="no_result"),
        _completed("d", robustness_score=1.0, passed_gates=True),
        _completed(
            "e",
            robustness_score=1.0,
            passed_gates=False,
            gate_flags=["few_oos_trades"],
            oos_trades=1,
        ),
    ]
    for result in cases:
        value = candidate_fitness(result, genome, genetic)
        assert math.isfinite(value)
        assert "inf" not in repr(value)
