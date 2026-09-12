"""Tests for genome signal-activity probes and repair (WO53)."""

from __future__ import annotations

import random

import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.genome.activity import (
    genome_signal_activity,
    population_tradeable_fraction,
    repair_genome,
)
from q_backend.backtesting.genome.operators import build_initial_population
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.registry_fixtures import REGISTRY_GENOME_FIXTURES
from q_backend.backtesting.genome.schema import Genome


def _oscillating_ohlcv(rows: int = 500) -> pd.DataFrame:
    prices = [100.0 + 10.0 * ((i % 40) - 20) / 20.0 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1000] * rows,
        }
    )


def _trending_ohlcv(rows: int = 300) -> pd.DataFrame:
    prices = [100.0 + i * 0.4 + (i % 7) * 0.2 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1000] * rows,
        }
    )


def _dead_crossover_genome() -> Genome:
    data = dict(REGISTRY_GENOME_FIXTURES["MACrossover"])
    data["nodes"] = [dict(node) for node in data["nodes"]]
    for node in data["nodes"]:
        if node["kind"] == "cmp.cross_above":
            node["params"] = {"threshold": 500.0}
        if node["kind"] == "cmp.cross_below":
            node["params"] = {"threshold": -500.0}
    return Genome.model_validate(data)


def test_probe_dead_threshold_reports_not_tradeable():
    df = _trending_ohlcv()
    stats = genome_signal_activity(_dead_crossover_genome(), df, min_signals=1)
    assert stats.n_entries == 0
    assert not stats.is_tradeable


def test_probe_registry_crossover_reports_tradeable():
    df = _oscillating_ohlcv()
    genome = Genome.model_validate(REGISTRY_GENOME_FIXTURES["MACrossover"])
    stats = genome_signal_activity(genome, df, min_signals=1)
    assert stats.n_entries > 0
    assert stats.is_tradeable


def test_repair_dead_genome_becomes_tradeable():
    df = _trending_ohlcv()
    rng = random.Random(11)
    repaired = repair_genome(
        rng,
        _dead_crossover_genome(),
        df,
        min_signals=1,
        max_nodes=24,
        max_depth=12,
        max_attempts=8,
    )
    stats = genome_signal_activity(repaired, df, min_signals=1)
    assert stats.is_tradeable


def test_threshold_bounds_allow_diff_centered_values():
    bounds = GENOME_PARAM_BOUNDS["threshold"]
    rng = random.Random(42)
    for _ in range(100):
        value = rng.uniform(bounds.min, bounds.max)
        assert bounds.min <= value <= bounds.max
        assert bounds.min <= 0.0 <= bounds.max


def test_generation_viability_lift_with_probe_enabled():
    df = _oscillating_ohlcv()
    seed = 2025
    disabled = build_initial_population(
        random.Random(seed),
        population_size=40,
        max_nodes=24,
        max_depth=12,
        ohlcv=None,
        min_seed_signals=0,
    )
    enabled = build_initial_population(
        random.Random(seed),
        population_size=40,
        max_nodes=24,
        max_depth=12,
        ohlcv=df,
        min_seed_signals=1,
        repair_max_attempts=8,
    )
    disabled_fraction = population_tradeable_fraction(disabled, df, min_signals=1, max_nodes=24, max_depth=12)
    enabled_fraction = population_tradeable_fraction(enabled, df, min_signals=1, max_nodes=24, max_depth=12)
    assert enabled_fraction > disabled_fraction
    assert enabled_fraction >= 0.8
