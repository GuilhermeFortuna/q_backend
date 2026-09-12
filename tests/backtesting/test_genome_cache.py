"""Interpreter column cache tests."""

from unittest.mock import patch

import pandas as pd

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.registry_fixtures import (
    MA_CROSSOVER_GENOME,
    REGISTRY_DEFAULT_PARAMS,
)


def _minimal_df() -> pd.DataFrame:
    index = pd.date_range("2023-01-01", periods=40, freq="h", tz="UTC")
    close = pd.Series(range(100, 140), index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def test_cache_keys_differ_only_on_changed_param_subgraph():
    params_a = {**REGISTRY_DEFAULT_PARAMS["MACrossover"], "short_period": 10}
    params_b = {**REGISTRY_DEFAULT_PARAMS["MACrossover"], "short_period": 12}

    plan_a = CompositeStrategy(genome=MA_CROSSOVER_GENOME, params=params_a, symbol="TEST").plan
    plan_b = CompositeStrategy(genome=MA_CROSSOVER_GENOME, params=params_b, symbol="TEST").plan

    keys_a = {node.node.id: node.cache_key for node in plan_a.sorted_nodes}
    keys_b = {node.node.id: node.cache_key for node in plan_b.sorted_nodes}

    assert keys_a["n1"] == keys_b["n1"]
    assert keys_a["n3"] == keys_b["n3"]
    assert keys_a["n2"] != keys_b["n2"]
    assert keys_a["n4"] != keys_b["n4"]
    assert keys_a["n5"] != keys_b["n5"]
    assert keys_a["n6"] != keys_b["n6"]


def test_compute_indicators_reuses_cached_nodes():
    params_a = {**REGISTRY_DEFAULT_PARAMS["MACrossover"], "short_period": 10}
    params_b = {**REGISTRY_DEFAULT_PARAMS["MACrossover"], "short_period": 12}

    df = _minimal_df()
    warmed = CompositeStrategy(genome=MA_CROSSOVER_GENOME, params=params_a, symbol="TEST")
    warmed.compute_indicators(df.copy())

    strategy = CompositeStrategy(genome=MA_CROSSOVER_GENOME, params=params_b, symbol="TEST")
    strategy._series_cache = dict(warmed._series_cache)
    strategy._last_data_len = len(df)

    evaluated: list[str] = []
    original_evaluate = CompositeStrategy._evaluate_node

    def recording_evaluate(self, frame, compiled):
        evaluated.append(compiled.node.id)
        return original_evaluate(self, frame, compiled)

    with patch.object(CompositeStrategy, "_evaluate_node", recording_evaluate):
        strategy.compute_indicators(df.copy())

    assert "n1" not in evaluated
    assert "n3" not in evaluated
    assert "n2" in evaluated
