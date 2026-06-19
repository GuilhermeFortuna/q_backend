"""Tests for genetic pre-screen before walk-forward (WO53)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome
from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.genetic_search import (
    GeneticCandidateProvider,
    GeneticStrategySearchOrchestrator,
    candidate_fitness,
    select_search_orchestrator,
)
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import (
    CandidateResult,
    GeneticSearchConfig,
    StrategySearchConfig,
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


def _config(
    *,
    start: datetime,
    end: datetime,
    init_seed: int = 7,
    prescreen_min_signals: int = 1,
    min_seed_signals: int = 0,
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
            name="prescreen",
            n_trials=1,
            seed=42,
            storage={"type": "memory"},
        ),
        include_risk_search=False,
        genetic=GeneticSearchConfig(
            population_size=10,
            generations=2,
            elite_count=1,
            init_seed=init_seed,
            crossover_rate=0.0,
            mutation_rate=0.0,
            tournament_size=2,
            max_nodes=12,
            max_depth=8,
            prescreen_min_signals=prescreen_min_signals,
            min_seed_signals=min_seed_signals,
        ),
    )


def _dead_genome(genome_id: str) -> Genome:
    data = dict(REGISTRY_GENOME_FIXTURES["MACrossover"])
    data["genome_id"] = genome_id
    data["nodes"] = [dict(node) for node in data["nodes"]]
    for node in data["nodes"]:
        if node["kind"] == "cmp.cross_above":
            node["params"] = {"threshold": 500.0}
        if node["kind"] == "cmp.cross_below":
            node["params"] = {"threshold": -500.0}
    return Genome.model_validate(data)


def _fake_evaluate(candidate, *args, **kwargs) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate.candidate_id,
        strategy=candidate.strategy,
        status="completed",
        passed_gates=False,
        robustness_score=1.0,
        objective_value=1.0,
        gate_flags=["few_oos_trades"],
        oos_metrics={"total_trades": 1},
        completed_windows=2,
        window_count=2,
    )


def test_prescreen_skips_evaluate_candidate_for_dead_genome():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _config(
        start=start,
        end=end,
        prescreen_min_signals=1,
        min_seed_signals=1,
    )
    provider = GeneticCandidateProvider(
        config.genetic,
        config,
        probe_df=full_df,
    )
    dead = _dead_genome(provider.population[0].genome_id)
    provider._population[0] = dead
    provider._genome_by_id[dead.genome_id] = dead
    orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)

    with patch(
        "q_backend.optimization.genetic_search.evaluate_candidate",
        side_effect=_fake_evaluate,
    ) as evaluate_mock:
        result = orchestrator.run()

    assert evaluate_mock.call_count == 19
    prescreened = [
        candidate
        for generation in orchestrator.generations
        for candidate in generation
        if candidate.error == "pre-screen: no in-sample signals"
    ]
    assert len(prescreened) >= 1
    assert orchestrator.generations[0][0].error == "pre-screen: no in-sample signals"
    assert result.candidates


def test_prescreened_no_result_gets_finite_graded_fitness():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _config(start=start, end=end, min_seed_signals=1)
    provider = GeneticCandidateProvider(config.genetic, config, probe_df=full_df)
    dead = _dead_genome(provider.population[0].genome_id)
    provider._population[0] = dead
    provider._genome_by_id[dead.genome_id] = dead
    orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)

    with patch(
        "q_backend.optimization.genetic_search.evaluate_candidate",
        side_effect=_fake_evaluate,
    ) as evaluate_mock:
        orchestrator.run()

    assert evaluate_mock.call_count == 19
    prescreened = orchestrator.generations[0][0]
    genome = provider.population[0]
    fitness = candidate_fitness(prescreened, genome, config.genetic)
    assert math.isfinite(fitness)
    assert prescreened.status == "no_result"


def test_single_get_ohlcv_with_prescreen_and_probe():
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
    config = _config(start=start, end=end, min_seed_signals=1)
    orchestrator = select_search_orchestrator(config, runner)
    orchestrator.run()
    service.get_ohlcv.assert_called_once()


def test_prescreen_determinism_same_seed_same_prescreen_decisions():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)

    def run_once() -> list[str]:
        runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
        config = _config(start=start, end=end, init_seed=99)
        provider = GeneticCandidateProvider(
            config.genetic,
            config,
            probe_df=full_df,
        )
        orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)
        orchestrator.run()
        return [
            candidate.error or candidate.status
            for candidate in orchestrator.generations[0]
        ]

    assert run_once() == run_once()
