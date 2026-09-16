"""Tick chart baseline regressions for Q-030.

Pins chart payloads (bars and indicator values) before the migration to q_core.
Goldens live in ``tests/backtesting/goldens/tick_chart/<case>.json``.
Regenerated only with ``--regen-goldens``.
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest

from backtesting.test_goldens import _canonical_json, _diff, synthetic_ticks
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays, TickStrategy

CHART_GOLDENS_DIR = Path(__file__).parents[1] / "goldens" / "tick_chart"


@dataclass
class ChartCase:
    name: str
    description: str
    make_ticks: Callable[[], TickArrays]
    make_strategy: Callable[[], TickStrategy]
    display_timeframe: str = "M1"


def _make_golden_strategy() -> TickStrategy:
    return build_tick_strategy(
        "TickMaBreakout",
        {
            "short_period": 20,
            "long_period": 50,
            "sl_points": 0.3,
            "tp_points": 0.6,
            "threshold": 0.0,
        },
        "SYNTH",
    )


def _make_zero_last_ticks() -> TickArrays:
    base = synthetic_ticks()
    return TickArrays(
        time_msc=base.time_msc,
        bid=base.bid,
        ask=base.ask,
        last=np.zeros_like(base.last),
        volume=base.volume,
    )


def _make_sparse_40d_ticks() -> TickArrays:
    span_40d = 40 * 86_400_000
    base_msc = 1_700_000_000_000
    times = base_msc + np.array([0, 10 * 86_400_000, 20 * 86_400_000, span_40d], dtype=np.int64)
    prices = np.array([100.0, 102.0, 101.0, 105.0], dtype=np.float64)
    return TickArrays(
        time_msc=times,
        bid=prices - 0.01,
        ask=prices + 0.01,
        last=prices,
        volume=np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float64),
    )


def _make_nan_warmup_ticks() -> TickArrays:
    prices = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], dtype=np.float64)
    n = len(prices)
    times = 1_700_000_000_000 + np.arange(n, dtype=np.int64) * 60_000
    return TickArrays(
        time_msc=times,
        bid=prices - 0.01,
        ask=prices + 0.01,
        last=prices,
        volume=np.ones(n, dtype=np.float64),
    )


def _make_small_period_strategy() -> TickStrategy:
    return build_tick_strategy(
        "TickMaBreakout",
        {
            "short_period": 2,
            "long_period": 3,
            "threshold": 0.0,
            "sl_points": 0.0,
            "tp_points": 0.0,
        },
        "SYNTH",
    )


CHART_CASES: dict[str, ChartCase] = {
    case.name: case
    for case in [
        ChartCase(
            name="m1_golden",
            description="M1 over the golden synthetic tick stream with TickMaBreakout",
            make_ticks=synthetic_ticks,
            make_strategy=_make_golden_strategy,
            display_timeframe="M1",
        ),
        ChartCase(
            name="m15_golden",
            description="M15 over the golden synthetic tick stream with TickMaBreakout",
            make_ticks=synthetic_ticks,
            make_strategy=_make_golden_strategy,
            display_timeframe="M15",
        ),
        ChartCase(
            name="all_zero_last",
            description="M1 over a tick stream whose last prices are all zero (falls back to bid/ask midpoint)",
            make_ticks=_make_zero_last_ticks,
            make_strategy=_make_golden_strategy,
            display_timeframe="M1",
        ),
        ChartCase(
            name="sparse_40_days_doubling",
            description="Sparse 40-day stream that doubles the M1 interval to 120s (M2)",
            make_ticks=_make_sparse_40d_ticks,
            make_strategy=_make_small_period_strategy,
            display_timeframe="M1",
        ),
        ChartCase(
            name="nan_warmup_indicators",
            description="TickMaBreakout indicators with NaN warm-up values sampled as None at bar ends",
            make_ticks=_make_nan_warmup_ticks,
            make_strategy=_make_small_period_strategy,
            display_timeframe="M1",
        ),
    ]
}


def run_chart_case(name: str) -> dict[str, Any]:
    case = CHART_CASES[name]
    ticks = case.make_ticks()
    strategy = case.make_strategy()
    chart = serialize_tick_chart_data(ticks, strategy, display_timeframe=case.display_timeframe)
    return {
        "case": case.name,
        "description": case.description,
        "display_timeframe": case.display_timeframe,
        "bar_count": len(chart["bars"]),
        "bars": chart["bars"],
        "indicators": chart["indicators"],
    }


def _golden_path(name: str) -> Path:
    return CHART_GOLDENS_DIR / f"{name}.json"


def _assert_or_regen(name: str, payload: dict[str, Any], regen: bool) -> None:
    actual = _canonical_json(payload)
    path = _golden_path(name)
    if regen:
        CHART_GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), (
        f"Missing golden {path}. Generate it with:\n"
        f"  uv run pytest tests/backtesting/tick/test_tick_chart_baseline.py --regen-goldens"
    )
    expected = path.read_text()
    if expected != actual:
        raise AssertionError(
            f"Golden mismatch for chart case {name!r}. If this change is intended, "
            f"regenerate with --regen-goldens and justify the diff in your commit.\n\n" + _diff(name, expected, actual)
        )


@pytest.mark.parametrize("name", list(CHART_CASES))
def test_chart_golden(name: str, regen_goldens: bool) -> None:
    _assert_or_regen(name, run_chart_case(name), regen_goldens)


@pytest.mark.parametrize("name", list(CHART_CASES))
def test_chart_determinism_double_run(name: str) -> None:
    first = _canonical_json(run_chart_case(name))
    second = _canonical_json(run_chart_case(name))
    assert first == second, f"Nondeterministic chart output for {name!r}"


def test_tamper_produces_readable_diff() -> None:
    name = "m1_golden"
    payload = copy.deepcopy(run_chart_case(name))
    assert len(payload["bars"]) > 0
    payload["bars"][0]["volume"] += 1

    with pytest.raises(AssertionError) as excinfo:
        _assert_or_regen(name, payload, regen=False)
    message = str(excinfo.value)
    assert "Golden mismatch" in message
    assert '"volume"' in message
