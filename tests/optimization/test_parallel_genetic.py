"""Tests for parallel genetic generation evaluation (WO54)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.genetic_parallel import (
    _init_genetic_worker,
    eval_config_for_parallel_workers,
    evaluate_generation_parallel,
)
from q_backend.optimization.genetic_search import (
    GeneticCandidateProvider,
    GeneticStrategySearchOrchestrator,
    select_search_orchestrator,
)
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.strategy_search import (
    GeneticSearchConfig,
    StrategySearchConfig,
    _rank_results,
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


def _config(
    *,
    start: datetime,
    end: datetime,
    init_seed: int = 42,
    max_workers: int | None = 1,
    population_size: int = 10,
    generations: int = 2,
    n_trials: int = 1,
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
            max_workers=4,
        ),
        study=StudyConfig(
            name="parallel_genetic",
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
            crossover_rate=0.0,
            mutation_rate=0.0,
            tournament_size=2,
            max_nodes=12,
            max_depth=8,
            max_workers=max_workers,
            prescreen_min_signals=0,
            min_seed_signals=0,
        ),
    )


def test_serial_parallel_parity_same_seed():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])

    def run_once(max_workers: int | None) -> tuple[list[str], list[str]]:
        config = _config(start=start, end=end, init_seed=77, max_workers=max_workers)
        provider = GeneticCandidateProvider(
            config.genetic,
            config,
            probe_df=full_df,
        )
        orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)
        orchestrator.run()
        ids = [g.genome_id for g in provider.initial_population]
        for generation in orchestrator.generations:
            provider.report(generation)
            ids.extend(g.genome_id for g in provider.population)
        ranked = _rank_results(orchestrator.generations[-1])
        return ids, [c.candidate_id for c in ranked]

    serial_ids, serial_ranked = run_once(1)
    parallel_ids, parallel_ranked = run_once(2)
    assert serial_ids == parallel_ids
    assert serial_ranked == parallel_ranked


def test_worker_initializer_materializes_frame_once_per_worker():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 60)
    config = _config(start=start, end=end, max_workers=2, population_size=10)
    provider = GeneticCandidateProvider(config.genetic, config, probe_df=full_df)
    candidates = provider.candidates()

    with patch("q_backend.optimization.genetic_parallel.ProcessPoolExecutor") as pool_cls:
        pool = pool_cls.return_value.__enter__.return_value
        evaluate_generation_parallel(
            candidates,
            config,
            full_df,
            max_workers=2,
            generation=1,
            total_generations=2,
        )

    pool_cls.assert_called_once()
    _, kwargs = pool_cls.call_args
    assert kwargs["initializer"] is _init_genetic_worker
    assert kwargs["initargs"] == (full_df,)
    assert kwargs["max_workers"] == resolve_worker_count(2, len(candidates))
    assert pool.submit.call_count == len(candidates)


def test_parallel_workers_force_window_max_workers_to_one():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    config = _config(start=start, end=end, max_workers=2)
    worker_config = eval_config_for_parallel_workers(config)
    assert worker_config.walkforward.max_workers == 1
    assert config.walkforward.max_workers == 4


def test_parallel_cancellation_returns_partial_generation():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _config(start=start, end=end, max_workers=1, population_size=10)
    provider = GeneticCandidateProvider(config.genetic, config, probe_df=full_df)
    orchestrator = GeneticStrategySearchOrchestrator(config, provider, runner)

    completed = [0]

    def should_stop() -> bool:
        return completed[0] >= 2

    def progress_callback(progress) -> None:
        if progress.phase == "done":
            completed[0] += 1

    result = orchestrator.run(
        progress_callback=progress_callback,
        should_stop=should_stop,
    )
    assert len(orchestrator.generations) >= 1
    assert len(orchestrator.generations[0]) < config.genetic.population_size
    assert result.candidates


def test_parallel_smoke_completes_multi_generation_run():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _config(start=start, end=end, max_workers=2, population_size=10, generations=2)
    orchestrator = select_search_orchestrator(config, runner)
    result = orchestrator.run()
    assert len(orchestrator.generations) == 2
    assert result.candidates


def test_parallel_speedup_smoke_not_timing_threshold():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _config(
        start=start,
        end=end,
        max_workers=2,
        population_size=10,
        generations=2,
        n_trials=1,
    )

    serial_config = config.model_copy(update={"genetic": config.genetic.model_copy(update={"max_workers": 1})})
    parallel_config = config.model_copy(update={"genetic": config.genetic.model_copy(update={"max_workers": 2})})

    t0 = time.perf_counter()
    select_search_orchestrator(serial_config, runner).run()
    serial_elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
    select_search_orchestrator(parallel_config, runner).run()
    parallel_elapsed = time.perf_counter() - t1

    assert serial_elapsed > 0
    assert parallel_elapsed > 0
