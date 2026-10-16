"""Indicator catalog coverage tests (acceptance criterion 3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.indicator_catalog import (
    CATALOG_BY_ID,
    CATALOG_EXCLUDED_KERNELS,
    INDICATOR_CATALOG,
    MAX_BARS,
    charted_kernels,
    published_q_core_kernels,
    validate_params,
)


def test_every_kernel_is_charted_or_excluded():
    published = published_q_core_kernels()
    charted = charted_kernels()
    assert published == charted | CATALOG_EXCLUDED_KERNELS
    assert CATALOG_EXCLUDED_KERNELS == frozenset({"clip", "volume_at_price"})


def _frame(count: int = 1_500) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, count)))
    open_ = close * np.exp(rng.normal(0.0, 0.001, count))
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "volume": rng.integers(500, 5_000, count).astype(float),
        }
    )


_BAR_ENTRIES = [entry for entry in INDICATOR_CATALOG if entry.required_data == "bars"]


@pytest.mark.parametrize("entry", _BAR_ENTRIES, ids=lambda entry: entry.id)
def test_declared_warmup_makes_values_independent_of_the_frame_start(entry):
    """After its declared warm-up an indicator has a value, and the value no longer depends
    on how much earlier history was loaded (exactly, or within the 1% seed weight for a
    recursive average)."""
    params = validate_params(entry, {})
    warmup = entry.warmup_bars(params)
    frame = _frame()
    cut = 400
    full = entry.compute(frame, params)
    late = entry.compute(frame.iloc[cut:].reset_index(drop=True), params)
    for output in entry.outputs:
        whole = full[output.key].to_numpy()
        part = late[output.key].to_numpy()
        assert np.isfinite(part[warmup:]).all(), "missing values after the declared warm-up"
        scale = np.nanmax(np.abs(whole))
        assert np.abs(part[warmup:] - whole[cut + warmup :]).max() <= 0.01 * scale


@pytest.mark.parametrize("entry", INDICATOR_CATALOG, ids=lambda entry: entry.id)
def test_every_numeric_parameter_is_bounded(entry):
    for spec in entry.params:
        if spec.type in ("int", "float"):
            assert spec.minimum is not None and spec.maximum is not None, spec.name
            assert spec.minimum <= spec.default <= spec.maximum, spec.name


def test_validate_params_rejects_what_it_cannot_compute():
    sma = CATALOG_BY_ID["sma"]
    assert validate_params(sma, {"period": 20.0}) == {"period": 20}
    for bad in ({"period": 20.5}, {"period": True}, {"period": "abc"}, {"period": MAX_BARS + 1}, {"length": 5}):
        with pytest.raises(ValueError, match=next(iter(bad))):
            validate_params(sma, bad)
    with pytest.raises(ValueError, match="half_life"):
        validate_params(CATALOG_BY_ID["per_bar_ewma_vol"], {"half_life": float("inf")})
