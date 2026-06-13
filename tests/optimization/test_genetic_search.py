"""Tests for genetic strategy search (WO39)."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.genome.operators import (
    build_initial_population,
    clone_genome,
    crossover_genomes,
    draw_valid_child,
    mutate_genome,
)
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import validate_genome
from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.genetic_search import (
    GeneticCandidateProvider,
    GeneticStrategySearchOrchestrator,
    candidate_fitness,
    select_search_orchestrator,
)
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    ObjectiveConfig,
    ObjectiveMode,
    SearchSpaceConfig,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GeneticSearchConfig,
    SearchCandidate,
    StrategySearchConfig,
    StrategySearchRunner,
    evaluate_candidate,
)
from q_backend.optimization.walkforward import WalkForwardConfig


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)


def _make_intraday_ohlcv(start: datetime, days: int) -> pd.DataFrame:
    rows = []
    price = 100.0
    for day in range(days):
        for hour in (0, 6, 12, 18):
            timestamp = start + timedelta(days=day, hours=hour)
            drift = 0.1 if day % 10 < 5 else -0.05
            price = max(50.0, price + drift)
            rows.append(
                {
                    "time": timestamp,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 1000,
                }
            )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def _bars_from_df(full_df: pd.DataFrame) -> list:
    class Bar:
        def __init__(self, row, timestamp):
            self._row = row
            self._timestamp = timestamp

        def model_dump(self):
            return {
                "time": self._timestamp,
                "open": self._row.open,
                "high": self._row.high,
                "low": self._row.low,
                "close": self._row.close,
                "volume": self._row.volume,
            }

    return [Bar(row, idx.to_pydatetime()) for idx, row in full_df.iterrows()]


def _narrow_risk_space() -> dict:
    return {
        "type": CategoricalParam(type="categorical", choices=["fixed_quantity"]),
        "quantity": FloatParam(type="float", low=1.0, high=1.0),
    }


def _genetic_search_config(
    *,
    start: datetime,
    end: datetime,
    init_seed: int = 123,
    population_size: int = 10,
    generations: int = 2,
    n_trials: int = 2,
) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "timeframe": "D1",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "initial_capital": 10_000.0,
            "point_value": 1.0,
            "strategy": "CompositeStrategy",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(
            train_days=30,
            test_days=15,
            mode="rolling",
            min_windows=2,
            max_workers=1,
        ),
        study=StudyConfig(
            name="genetic_search_e2e",
            n_trials=n_trials,
            seed=42,
            storage={"type": "memory"},
        ),
        include_risk_search=False,
        genetic=GeneticSearchConfig(
            population_size=population_size,
            generations=generations,
            elite_count=1,
            init_seed=init_seed,
            crossover_rate=0.7,
            mutation_rate=0.2,
            tournament_size=2,
            max_nodes=12,
            max_depth=8,
        ),
    )


def _completed_result(
    candidate_id: str,
    *,
    robustness_score: float,
    passed_gates: bool = True,
) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        strategy="CompositeStrategy",
        status="completed",
        passed_gates=passed_gates,
        objective_value=robustness_score,
        robustness_score=robustness_score,
        oos_metrics={"total_trades": 20},
        completed_windows=2,
        window_count=2,
    )


def test_evaluate_candidate_signature_unchanged():
    signature = inspect.signature(evaluate_candidate)
    assert "ohlcv" in signature.parameters
    assert "progress_callback" in signature.parameters
    assert "should_stop" in signature.parameters


def test_select_search_orchestrator_returns_registry_runner_by_default():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    config = _genetic_search_config(start=start, end=end)
    config = config.model_copy(update={"genetic": None})
    runner = DefaultBacktestRunner(data_provider=lambda cfg: pd.DataFrame())
    orchestrator = select_search_orchestrator(config, runner)
    assert isinstance(orchestrator, StrategySearchRunner)


def test_select_search_orchestrator_returns_genetic_orchestrator():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    config = _genetic_search_config(start=start, end=end)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: pd.DataFrame())
    orchestrator = select_search_orchestrator(config, runner)
    assert isinstance(orchestrator, GeneticStrategySearchOrchestrator)


def test_initial_population_is_valid_and_mixed():
    import random

    rng = random.Random(7)
    population = build_initial_population(
        rng,
        population_size=10,
        max_nodes=12,
        max_depth=8,
    )
    assert len(population) == 10
    seed_like = sum(
        1
        for genome in population
        if genome.metadata.get("equivalent_registry") or genome.genome_id.startswith("gen0-seed")
    )
    random_like = sum(1 for genome in population if genome.metadata.get("origin") == "random")
    assert seed_like >= 4
    assert random_like >= 4
    for genome in population:
        validate_genome(genome, max_depth=8, max_node_count=12)
        assert len(genome.nodes) <= 12


def test_complexity_penalty_prefers_simpler_genome():
    genetic = GeneticSearchConfig()
    simple = Genome.model_validate(REGISTRY_GENOME_FIXTURES["DonchianBreakout"])
    complex_genome = Genome.model_validate(REGISTRY_GENOME_FIXTURES["MACrossover"])
    result = _completed_result("x", robustness_score=10.0)
    simple_fitness = candidate_fitness(result, simple, genetic)
    complex_fitness = candidate_fitness(result, complex_genome, genetic)
    assert simple_fitness > complex_fitness


def test_provider_deterministic_initial_population():
    config_a = GeneticSearchConfig(population_size=10, init_seed=999)
    config_b = GeneticSearchConfig(population_size=10, init_seed=999)
    search = StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study=StudyConfig(name="seed", n_trials=1),
    )
    provider_a = GeneticCandidateProvider(config_a, search)
    provider_b = GeneticCandidateProvider(config_b, search)
    ids_a = [genome.genome_id for genome in provider_a.population]
    ids_b = [genome.genome_id for genome in provider_b.population]
    assert ids_a == ids_b


def test_provider_elitism_is_monotonic():
    genetic = GeneticSearchConfig(
        population_size=10,
        elite_count=1,
        init_seed=11,
        crossover_rate=0.0,
        mutation_rate=0.0,
    )
    search = StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study=StudyConfig(name="elite", n_trials=1),
    )
    provider = GeneticCandidateProvider(genetic, search)
    best_scores: list[float] = []

    for generation in range(3):
        candidates = provider.candidates()
        results = []
        for index, candidate in enumerate(candidates):
            score = float(index + generation)
            results.append(
                _completed_result(candidate.candidate_id, robustness_score=score)
            )
        best_scores.append(max(candidate_fitness(r, provider.population[i], genetic) for i, r in enumerate(results)))
        provider.report(results)

    assert best_scores[1] >= best_scores[0]
    assert best_scores[2] >= best_scores[1]


def test_operators_preserve_validity():
    import random

    rng = random.Random(5)
    population = build_initial_population(
        rng, population_size=6, max_nodes=12, max_depth=8
    )
    for _ in range(20):
        parent_a = rng.choice(population)
        parent_b = rng.choice(population)
        child = draw_valid_child(
            rng,
            lambda: crossover_genomes(
                rng, parent_a, parent_b, max_nodes=12, max_depth=8
            ),
        )
        validate_genome(child, max_depth=8, max_node_count=12)
        child = draw_valid_child(
            rng,
            lambda: mutate_genome(rng, child, max_nodes=12, max_depth=8),
        )
        validate_genome(child, max_depth=8, max_node_count=12)


def test_genetic_search_end_to_end():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _genetic_search_config(
        start=start,
        end=end,
        population_size=10,
        generations=2,
        n_trials=2,
        init_seed=42,
    )
    provider = GeneticCandidateProvider(config.genetic, config)
    orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)
    result = orchestrator.run()

    assert len(orchestrator.generations) == 2
    assert len(orchestrator.generations[0]) == 10
    assert len(result.candidates) == 10
    assert all(candidate.strategy == "CompositeStrategy" for candidate in result.candidates)
    assert result.objective_mode == ObjectiveMode.MAXIMIZE_NET_PROFIT


def test_single_get_ohlcv_across_multi_generation_search():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    service = MagicMock()
    service.get_ohlcv.return_value = _bars_from_df(full_df)

    runner = DefaultBacktestRunner.from_market_data_sliced(
        service,
        symbol="TEST",
        timeframe="D1",
        start=start,
        end=end,
    )
    config = _genetic_search_config(
        start=start,
        end=end,
        population_size=10,
        generations=2,
        n_trials=1,
        init_seed=7,
    )
    orchestrator = select_search_orchestrator(config, runner)
    orchestrator.run()

    service.get_ohlcv.assert_called_once()


def test_should_stop_between_generations():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _genetic_search_config(
        start=start,
        end=end,
        population_size=10,
        generations=3,
        n_trials=1,
        init_seed=3,
    )
    provider = GeneticCandidateProvider(config.genetic, config)
    orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)

    stop_after_first_gen = [False]

    def progress_callback(progress) -> None:
        if progress.generation == 1 and progress.phase == "done":
            stop_after_first_gen[0] = True

    def should_stop() -> bool:
        return stop_after_first_gen[0]

    result = orchestrator.run(
        progress_callback=progress_callback,
        should_stop=should_stop,
    )

    assert len(orchestrator.generations) == 1
    assert result.candidates


def test_provider_candidates_use_composite_strategy():
    genetic = GeneticSearchConfig(population_size=10, init_seed=1)
    search = StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study=StudyConfig(name="candidates", n_trials=1),
    )
    provider = GeneticCandidateProvider(genetic, search)
    candidates = provider.candidates()
    assert len(candidates) == 10
    assert all(candidate.strategy == "CompositeStrategy" for candidate in candidates)
    assert all("genome" in candidate.fixed_params for candidate in candidates)
    assert all(isinstance(candidate.search_space.strategy_params, dict) for candidate in candidates)


def test_deterministic_population_after_reports():
    genetic = GeneticSearchConfig(
        population_size=10,
        init_seed=2024,
        crossover_rate=0.0,
        mutation_rate=0.0,
    )
    search = StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "start": "2024-01-01",
            "end": "2024-06-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=10, test_days=5, min_windows=1),
        study=StudyConfig(name="determinism", n_trials=1),
    )

    def run_once() -> list[list[str]]:
        provider = GeneticCandidateProvider(genetic, search)
        history: list[list[str]] = [ [g.genome_id for g in provider.population] ]
        for _ in range(2):
            candidates = provider.candidates()
            results = [
                _completed_result(candidate.candidate_id, robustness_score=float(index))
                for index, candidate in enumerate(candidates)
            ]
            provider.report(results)
            history.append([g.genome_id for g in provider.population])
        return history

    assert run_once() == run_once()
