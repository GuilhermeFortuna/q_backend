"""Performance benchmarks for representative M15/H1 deployment fixtures."""

from __future__ import annotations

import statistics
import time

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator


def _synthetic_ohlcv(n: int = 260, *, freq: str = "h") -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


BENCHMARK_FIXTURES = (
    ("CCM$", "H1", "h", 400),
    ("WIN$", "H1", "h", 400),
    ("WDO$", "M15", "15min", 600),
)


@pytest.mark.parametrize("symbol,timeframe,freq,bars", BENCHMARK_FIXTURES)
def test_evaluator_phase_benchmark(symbol: str, timeframe: str, freq: str, bars: int):
    compiled = {
        "strategy": "MACrossover",
        "strategy_params": default_params_for("MACrossover"),
        "symbol": symbol,
        "timeframe": timeframe,
    }
    identity = StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config=compiled,
        config_hash=f"bench-{symbol}-{timeframe}",
        symbol=symbol,
        timeframe=timeframe,
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )
    data = _synthetic_ohlcv(bars, freq=freq)
    evaluator = StrategyEvaluator(deployment_id=f"bench-{symbol}", identity=identity)
    evaluator.seed_window(data.iloc[:-20])

    indicator_samples: list[float] = []
    evaluate_samples: list[float] = []
    start_idx = len(data) - 20
    for i in range(start_idx, len(data)):
        results = evaluator.ingest_completed_bars(data.iloc[i : i + 1])
        if not results:
            continue
        timing = results[0].timing
        indicator_samples.append(timing.indicators_ms)
        evaluate_samples.append(timing.evaluate_ms)

    # Documented readings for WO170 (synthetic local frames, no MT5 I/O).
    report = {
        "symbol": symbol,
        "timeframe": timeframe,
        "indicator_p50_ms": statistics.median(indicator_samples),
        "evaluate_p50_ms": statistics.median(evaluate_samples),
        "samples": len(indicator_samples),
    }
    print(f"BENCHMARK {report}")
    assert report["samples"] >= 10
    # Loose guardrail: incremental closed-bar eval should stay well under paper budget.
    assert report["indicator_p50_ms"] < 250.0
    assert report["evaluate_p50_ms"] < 50.0
