"""Unit tests for the q_core tick kernel bridge module (Q-030)."""

from __future__ import annotations

import numpy as np
import pytest

from backtesting.test_goldens import synthetic_ticks
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    FixedSafetyMarginPositionSizing,
    InverseVolatilityPositionSizing,
)
from q_backend.backtesting.tick.kernel import (
    check_tick_engine,
    simulate,
    simulate_config,
)
from q_backend.backtesting.tick.orders import (
    SIZING_FIXED_QUANTITY,
    SIZING_FIXED_SAFETY_MARGIN,
    kernel_sizing_params,
)


def _make_test_signals(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    direction = np.zeros(n, dtype=np.int8)
    direction[5] = 1
    direction[20] = -1
    direction[35] = 1
    sl = np.full(n, 0.5, dtype=np.float64)
    tp = np.full(n, 1.0, dtype=np.float64)
    return direction, sl, tp


def test_simulate_padded_nine_tuple_matches_simulate_config():
    ticks = synthetic_ticks(100)
    n = len(ticks.bid)
    direction, sl, tp = _make_test_signals(n)
    initial_capital = 10_000.0
    point_value = 1.0

    tuple_res = simulate(
        ticks.bid,
        ticks.ask,
        direction,
        sl,
        tp,
        initial_capital,
        point_value,
        sizing_mode=SIZING_FIXED_QUANTITY,
        sizing_a=1.0,
        sizing_b=0.0,
        sizing_c=0.0,
    )

    config_res = simulate_config(
        ticks.bid,
        ticks.ask,
        direction,
        sl,
        tp,
        initial_capital,
        point_value,
        sizing=FixedQuantityPositionSizing(quantity=1.0),
    )

    assert len(tuple_res) == 9
    entry_idx, exit_idx, entry_price, exit_price, trade_dir, qty, reason, trade_count, final_cap = tuple_res

    assert trade_count == len(config_res["entry_idx"])
    assert trade_count > 0
    assert final_cap == pytest.approx(config_res["final_capital"])

    arrays = [entry_idx, exit_idx, entry_price, exit_price, trade_dir, qty, reason]
    for arr in arrays:
        assert len(arr) == n
        # Zeros past trade_count
        assert np.all(arr[trade_count:] == 0)

    # Values up to trade_count match simulate_config
    np.testing.assert_array_equal(entry_idx[:trade_count], config_res["entry_idx"])
    np.testing.assert_array_equal(exit_idx[:trade_count], config_res["exit_idx"])
    np.testing.assert_allclose(entry_price[:trade_count], config_res["entry_price"])
    np.testing.assert_allclose(exit_price[:trade_count], config_res["exit_price"])
    np.testing.assert_array_equal(trade_dir[:trade_count], config_res["direction"])
    np.testing.assert_allclose(qty[:trade_count], config_res["quantity"])
    np.testing.assert_array_equal(reason[:trade_count], config_res["exit_reason"])


def test_fixed_safety_margin_float_min_contracts_truncates():
    ticks = synthetic_ticks(100)
    n = len(ticks.bid)
    direction, sl, tp = _make_test_signals(n)
    initial_capital = 10_000.0
    point_value = 1.0

    res_float = simulate(
        ticks.bid,
        ticks.ask,
        direction,
        sl,
        tp,
        initial_capital,
        point_value,
        sizing_mode=SIZING_FIXED_SAFETY_MARGIN,
        sizing_a=15_000.0,  # Capital < margin -> qty from capital is 0, but min_contracts triggers
        sizing_b=1.7,
        sizing_c=5.0,
    )

    res_int = simulate(
        ticks.bid,
        ticks.ask,
        direction,
        sl,
        tp,
        initial_capital,
        point_value,
        sizing_mode=SIZING_FIXED_SAFETY_MARGIN,
        sizing_a=15_000.0,
        sizing_b=1.0,
        sizing_c=5.0,
    )

    trade_count_float = res_float[7]
    trade_count_int = res_int[7]
    assert trade_count_float == trade_count_int
    assert trade_count_float > 0
    # Quantities up to trade_count should match
    np.testing.assert_allclose(res_float[5][:trade_count_float], res_int[5][:trade_count_int])
    assert res_float[8] == pytest.approx(res_int[8])


def test_kernel_sizing_params_rejects_unsupported_model():
    sizing = InverseVolatilityPositionSizing()
    with pytest.raises(ValueError, match="tick engine does not support position sizing type 'inverse_volatility'"):
        kernel_sizing_params(sizing)


def test_check_tick_engine_raises_on_missing_functions():
    class Stub:
        def version(self) -> str:
            return "2026.09.99"

        tick_simulate = None
        tick_day_bounds = None
        # missing tick_bars
        resolve_bar_ms = None
        sample_at_bar_ends = None

    with pytest.raises(ImportError) as excinfo:
        check_tick_engine(Stub())

    msg = str(excinfo.value)
    assert "tick_bars" in msg
    assert "2026.09.99" in msg


def test_no_tick_module_imports_numba():
    from pathlib import Path

    tick_root = Path(__file__).resolve().parents[3] / "src" / "q_backend" / "backtesting" / "tick"
    assert tick_root.is_dir()
    offenders = []
    for py_file in tick_root.rglob("*.py"):
        for line in py_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("import numba") or stripped.startswith("from numba"):
                offenders.append(py_file.relative_to(tick_root).as_posix())
                break
    assert offenders == []
