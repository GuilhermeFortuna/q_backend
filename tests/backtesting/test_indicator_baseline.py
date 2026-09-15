"""Indicator baseline regression suite comparing against committed pandas reference."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.moving_averages import compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)
from q_backend.backtesting.transforms import (
    compute_clip,
    compute_pct_change,
    compute_rolling_rank,
    compute_rolling_zscore,
)
from tests.fixtures.indicators.export_pandas_baseline import get_dataset

BASELINE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "indicators" / "pandas_baseline.json"

FUNCTION_MAP = {
    "compute_realized_vol": compute_realized_vol,
    "compute_yang_zhang": compute_yang_zhang,
    "compute_rsi": compute_rsi,
    "compute_bollinger_bands": compute_bollinger_bands,
    "compute_macd": compute_macd,
    "compute_donchian_channels": compute_donchian_channels,
    "compute_atr": compute_atr,
    "compute_ma": compute_ma,
    "compute_rolling_zscore": compute_rolling_zscore,
    "compute_rolling_rank": compute_rolling_rank,
    "compute_pct_change": compute_pct_change,
    "compute_clip": compute_clip,
}


def load_baseline() -> dict[str, Any]:
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _invoke_case(case: dict[str, Any]) -> tuple[list[pd.Series], pd.Index]:
    func_name = case["function"]
    dataset_name = case["dataset"]
    params = case["params"]
    ds = get_dataset(dataset_name)

    if func_name == "compute_yang_zhang":
        c = ds["close"]
        ref_idx = c.index
        res = compute_yang_zhang(ds["open"], ds["high"], ds["low"], c, **params)
        return [res], ref_idx
    elif func_name == "compute_atr":
        c = ds["close"]
        ref_idx = c.index
        res = compute_atr(ds["high"], ds["low"], c, **params)
        return [res], ref_idx
    elif func_name == "compute_donchian_channels":
        h = ds["high"]
        ref_idx = h.index
        upper, lower = compute_donchian_channels(h, ds["low"], **params)
        return [upper, lower], ref_idx
    elif func_name in ("compute_bollinger_bands", "compute_macd"):
        close_series = ds["close"] if isinstance(ds, pd.DataFrame) else ds
        ref_idx = close_series.index
        func = FUNCTION_MAP[func_name]
        res = func(close_series, **params)
        return list(res), ref_idx
    else:
        series = ds["close"] if isinstance(ds, pd.DataFrame) else ds
        ref_idx = series.index
        func = FUNCTION_MAP[func_name]
        res = func(series, **params)
        return [res], ref_idx


def _compare_series(
    actual: pd.Series,
    expected_dict: dict[str, Any],
    ref_idx: pd.Index,
    abs_tol: float = 1e-10,
    rel_tol: float = 1e-12,
    perturb: float = 0.0,
) -> float:
    # 1. Index identity
    try:
        from q_backend.backtesting import indicator_kernels

        is_delegated = True
    except (ImportError, ModuleNotFoundError):
        is_delegated = False

    if is_delegated:
        assert actual.index is ref_idx, f"Result index is not the input index object: {actual.index} vs {ref_idx}"
    else:
        assert actual.index.equals(ref_idx), f"Result index does not match input index: {actual.index} vs {ref_idx}"

    # 2. Name match
    expected_name = expected_dict["name"]
    assert actual.name == expected_name, f"Name mismatch: got {actual.name!r}, expected {expected_name!r}"

    # 3. Dtype match
    expected_dtype = expected_dict["dtype"]
    assert str(actual.dtype) == expected_dtype, f"Dtype mismatch: got {actual.dtype}, expected {expected_dtype}"

    # 4. Values length
    expected_vals = expected_dict["values"]
    assert len(actual) == len(expected_vals), f"Length mismatch: {len(actual)} vs {len(expected_vals)}"

    max_diff = 0.0
    perturbed = False
    for i, (act_val, exp_spec) in enumerate(zip(actual, expected_vals)):
        if exp_spec == "nan":
            assert pd.isna(act_val), f"Index {i}: expected NaN, got {act_val}"
            continue
        if exp_spec == "inf":
            assert np.isposinf(act_val), f"Index {i}: expected +inf, got {act_val}"
            continue
        if exp_spec == "-inf":
            assert np.isneginf(act_val), f"Index {i}: expected -inf, got {act_val}"
            continue

        exp_val = float(exp_spec)
        if perturb != 0.0 and not perturbed:
            exp_val += perturb
            perturbed = True

        assert not pd.isna(act_val), f"Index {i}: got unexpected NaN, expected {exp_val}"
        assert not np.isinf(act_val), f"Index {i}: got unexpected inf, expected {exp_val}"

        diff = abs(float(act_val) - exp_val)
        if diff > max_diff:
            max_diff = diff

        passes = (diff <= abs_tol) or (diff <= rel_tol * abs(exp_val))
        assert (
            passes
        ), f"Index {i}: diff {diff} exceeds tol (abs_tol={abs_tol}, rel_tol={rel_tol}) for act={act_val}, exp={exp_val}"

    return max_diff


def test_indicator_baseline_all_cases() -> None:
    baseline = load_baseline()
    abs_tol = baseline["metadata"]["policy"]["abs_tol"]
    rel_tol = baseline["metadata"]["policy"]["rel_tol"]
    cases = baseline["cases"]

    max_diffs: dict[str, float] = {}

    for case in cases:
        actual_series_list, ref_idx = _invoke_case(case)
        exp_outputs = case["outputs"]
        assert len(actual_series_list) == len(exp_outputs), f"Output count mismatch for {case['case_id']}"

        func_key = case["function"]
        if case["function"] == "compute_ma":
            func_key = f"compute_ma[{case['params']['ma_type']}]"

        for act_s, exp_s in zip(actual_series_list, exp_outputs):
            diff = _compare_series(act_s, exp_s, ref_idx, abs_tol=abs_tol, rel_tol=rel_tol)
            max_diffs[func_key] = max(max_diffs.get(func_key, 0.0), diff)

    print("\n--- Maximum Absolute Differences Per Function ---")
    for fn, md in sorted(max_diffs.items()):
        print(f"  {fn:30s}: max diff = {md:.2e}")
    print("-------------------------------------------------")


def test_baseline_negative_control() -> None:
    baseline = load_baseline()
    case = baseline["cases"][0]  # synthetic_realized_vol_w5
    actual_series_list, ref_idx = _invoke_case(case)

    # Perturbing first float expected value by 1e-6 must fail the assertion
    with pytest.raises(AssertionError) as exc_info:
        _compare_series(
            actual_series_list[0],
            case["outputs"][0],
            ref_idx,
            abs_tol=1e-10,
            rel_tol=1e-12,
            perturb=1e-6,
        )
    assert "exceeds tol" in str(exc_info.value)
