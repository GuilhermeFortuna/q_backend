"""
Causality guardrail for registered tick strategies (mirror of test_strategy_causality).
"""

import numpy as np
import pytest

import q_backend.backtesting.tick.strategies  # noqa: F401
from q_backend.backtesting.strategy_registry import (
    default_params_for,
    get_registered_strategy,
    list_registered_strategies,
)
from q_backend.backtesting.tick.strategy import TickArrays

TICK_STRATEGY_NAMES = [info.name for info in list_registered_strategies() if info.engine == "tick"]


def _synthetic_ticks(n: int = 500) -> TickArrays:
    rng = np.random.default_rng(20240610)
    steps = rng.normal(0.0, 0.1, size=n)
    last = 100.0 + np.cumsum(steps)
    spread = 0.02
    bid = last - spread / 2
    ask = last + spread / 2
    base_msc = 1_700_000_000_000
    time_msc = base_msc + np.arange(n, dtype=np.int64) * 1000
    volume = rng.uniform(1.0, 10.0, size=n)
    return TickArrays(
        time_msc=time_msc,
        bid=bid.astype(np.float64),
        ask=ask.astype(np.float64),
        last=last.astype(np.float64),
        volume=volume.astype(np.float64),
    )


def test_tick_registry_is_populated():
    assert TICK_STRATEGY_NAMES, "no tick strategies registered"


@pytest.mark.parametrize("strategy_name", TICK_STRATEGY_NAMES)
def test_tick_strategy_signals_are_causal(strategy_name):
    entry = get_registered_strategy(strategy_name)
    strategy = entry.build(default_params_for(strategy_name), "TEST")

    ticks = _synthetic_ticks()
    full = strategy.compute_signals(ticks)

    for k in (205, 300, len(ticks.time_msc) - 1):
        prefix = _synthetic_ticks(k)
        prefix_signals = strategy.compute_signals(prefix)

        for field in ("direction", "sl_points", "tp_points"):
            expected = getattr(full, field)[:k]
            actual = getattr(prefix_signals, field)
            np.testing.assert_array_equal(expected, actual)

        # Perturb future ticks on full frame; prefix index i must not change
        perturbed = _synthetic_ticks()
        perturbed.last[k:] += 50.0
        perturbed.bid[k:] += 50.0
        perturbed.ask[k:] += 50.0
        perturbed_signals = strategy.compute_signals(perturbed)

        for field in ("direction", "sl_points", "tp_points"):
            np.testing.assert_array_equal(
                getattr(full, field)[:k],
                getattr(perturbed_signals, field)[:k],
            )
