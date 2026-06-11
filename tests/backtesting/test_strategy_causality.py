"""
Guardrail tests that protect every registered strategy from look-ahead bias.

Look-ahead bias is the single most damaging class of backtesting bug: a
strategy that (even accidentally) reads future data will report results that
are impossible to reproduce in live trading. The original MA-crossover report
issue -- acting on a bar before it could really be acted upon -- is one
instance of this family.

Rather than eyeball each strategy, we assert the *structural* property that
makes the whole class of bugs impossible: causality. An indicator value at bar
``i`` may only depend on bars ``<= i``. We verify this by recomputing a
strategy's indicators over progressively shorter prefixes of the same data and
checking that the values for the shared earlier bars never change. If a
strategy peeks ahead (centered windows, ``shift(-n)``, whole-series min/max or
normalisation, etc.), removing the future bars would change the earlier rows
and the test fails.

This runs for *every* strategy in the registry, so a new strategy added later
(by a human or an AI agent) is covered automatically -- no extra wiring.
"""

import numpy as np
import pandas as pd
import pytest

# Importing the package triggers registration of every built-in strategy.
import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.strategy_registry import (
    default_params_for,
    get_registered_strategy,
    list_registered_strategies,
)

STRATEGY_NAMES = [
    info.name for info in list_registered_strategies() if info.engine == "candle"
]


def _synthetic_ohlcv(n: int = 260) -> pd.DataFrame:
    """A deterministic random-walk OHLCV frame large enough for long windows."""
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


def test_registry_is_populated():
    """Sanity check: the guardrail is actually exercising real strategies."""
    assert STRATEGY_NAMES, "no strategies registered -- causality test is a no-op"


@pytest.mark.parametrize("strategy_name", STRATEGY_NAMES)
def test_strategy_indicators_are_causal(strategy_name):
    entry = get_registered_strategy(strategy_name)
    strategy = entry.build(default_params_for(strategy_name), "TEST")

    data = _synthetic_ohlcv()
    full = strategy.compute_indicators(data)

    added_cols = [c for c in full.columns if c not in data.columns]
    assert added_cols, f"{strategy_name}: compute_indicators added no columns"

    # Cut points chosen to sit beyond the default long windows so the compared
    # rows contain real (non-NaN) indicator values, not just warm-up NaNs.
    for k in (205, 230, len(data) - 1):
        prefix = strategy.compute_indicators(data.iloc[:k].copy())

        for col in added_cols:
            assert col in prefix.columns, (
                f"{strategy_name}: column '{col}' missing when run on a prefix"
            )
            expected = full[col].iloc[:k]
            actual = prefix[col]

            if pd.api.types.is_bool_dtype(expected) or pd.api.types.is_bool_dtype(
                actual
            ):
                mismatches = (
                    expected.fillna(False).to_numpy()
                    != actual.fillna(False).to_numpy()
                )
                assert not mismatches.any(), (
                    f"{strategy_name}: signal column '{col}' changes at "
                    f"prefix length {k} -> look-ahead bias"
                )
            else:
                pd.testing.assert_series_equal(
                    expected,
                    actual,
                    check_names=False,
                    rtol=1e-9,
                    atol=1e-9,
                )
