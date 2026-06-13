"""Random valid genome causality property tests."""

import random

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import validate_genome


def _synthetic_ohlcv(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _random_genome(rng: random.Random) -> dict:
    threshold = rng.uniform(0.0, 5.0)
    short = rng.randint(5, 30)
    long = rng.randint(short + 5, 80)
    return {
        "version": 1,
        "genome_id": f"random-{rng.randint(0, 99999)}",
        "nodes": [
            {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
            {
                "id": "n2",
                "kind": "ind.ma",
                "params": {"period": short, "ma_type": "sma"},
                "inputs": ["n1"],
            },
            {
                "id": "n3",
                "kind": "ind.ma",
                "params": {"period": long, "ma_type": "ema"},
                "inputs": ["n1"],
            },
            {"id": "n4", "kind": "ind.diff", "params": {}, "inputs": ["n2", "n3"]},
            {
                "id": "n5",
                "kind": "cmp.cross_above",
                "params": {"threshold": threshold},
                "inputs": ["n4"],
            },
            {
                "id": "n6",
                "kind": "cmp.cross_below",
                "params": {"threshold": -threshold},
                "inputs": ["n4"],
            },
        ],
        "entry_long": {"ref": "n5"},
        "entry_short": {"ref": "n6"},
        "exit_long": {"ref": "n6"},
        "exit_short": {"ref": "n5"},
        "metadata": {},
    }


@pytest.mark.parametrize("seed", range(5))
def test_random_valid_genomes_are_causal(seed: int):
    rng = random.Random(seed)
    genome_data = _random_genome(rng)
    validate_genome(Genome.model_validate(genome_data))

    strategy = CompositeStrategy(genome=genome_data, params={}, symbol="TEST")
    data = _synthetic_ohlcv()
    full = strategy.compute_indicators(data)
    added_cols = [c for c in full.columns if c not in data.columns]

    for k in (205, 230, len(data) - 1):
        prefix = strategy.compute_indicators(data.iloc[:k].copy())
        for col in added_cols:
            expected = full[col].iloc[:k]
            actual = prefix[col]
            if pd.api.types.is_bool_dtype(expected) or pd.api.types.is_bool_dtype(actual):
                mismatches = (
                    expected.fillna(False).to_numpy() != actual.fillna(False).to_numpy()
                )
                assert not mismatches.any(), f"look-ahead in {col} at prefix {k}"
            else:
                pd.testing.assert_series_equal(
                    expected, actual, check_names=False, rtol=1e-9, atol=1e-9
                )
