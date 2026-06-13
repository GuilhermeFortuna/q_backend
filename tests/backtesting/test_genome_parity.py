"""Registry parity tests — CompositeStrategy vs hand-coded twins."""

import numpy as np
import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.strategy_registry import get_registered_strategy
from q_backend.backtesting.genome.registry_fixtures import (
    REGISTRY_DEFAULT_PARAMS,
    REGISTRY_GENOME_FIXTURES,
)


def _synthetic_ohlcv(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
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


SIGNAL_COLUMNS = ("buy_signal", "sell_signal", "exit_long_signal", "exit_short_signal")


@pytest.mark.parametrize("strategy_name", list(REGISTRY_GENOME_FIXTURES.keys()))
def test_registry_genome_signal_parity(strategy_name: str):
    params = REGISTRY_DEFAULT_PARAMS[strategy_name]
    registry = get_registered_strategy(strategy_name).build(params, "TEST")
    composite = CompositeStrategy(
        genome=REGISTRY_GENOME_FIXTURES[strategy_name],
        params=params,
        symbol="TEST",
    )

    data = _synthetic_ohlcv()
    registry_df = registry.compute_indicators(data)
    composite_df = composite.compute_indicators(data)

    for col in ("buy_signal", "sell_signal"):
        assert col in registry_df.columns
        assert col in composite_df.columns
        expected = registry_df[col].fillna(False).to_numpy()
        actual = composite_df[col].fillna(False).to_numpy()
        assert np.array_equal(expected, actual), f"{strategy_name}: {col} mismatch"

    if "exit_long_signal" in registry_df.columns:
        for col in ("exit_long_signal", "exit_short_signal"):
            expected = registry_df[col].fillna(False).to_numpy()
            actual = composite_df[col].fillna(False).to_numpy()
            assert np.array_equal(expected, actual), f"{strategy_name}: {col} mismatch"
