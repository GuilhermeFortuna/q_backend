"""Tests for the causal top/bottom detector exposed as ``indicators.top_bottom``."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import indicators


def _bars(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-09-01 09:00", periods=len(highs), freq="5min", tz=BRASILIA_TZ, name="time")
    return pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=idx)


@pytest.fixture
def swing_bars() -> pd.DataFrame:
    # With atr_period=1 and reversal_atr=1.0 the ATR equals each bar's true range, so the
    # pivots below were derived by hand from the rules in LiveSwingDetector.
    return _bars(
        highs=[10, 12, 13, 12, 9, 11, 14],
        lows=[8, 9, 10, 8, 7, 6, 7],
        closes=[9, 11, 12, 9, 8, 10, 13],
    )


def test_known_pivots_and_confirmation(swing_bars: pd.DataFrame) -> None:
    result = indicators.top_bottom(swing_bars, atr_period=1, reversal_atr=1.0)

    # Bottom at 8 (bar 0) confirmed on bar 1; top at 13 (bar 2) confirmed on bar 3;
    # bottom at 6 (bar 5) confirmed on bar 6.
    assert result["bottom"].iloc[1] == 8.0
    assert result["bottom_index"].iloc[1] == swing_bars.index[0]
    assert result["top"].iloc[3] == 13.0
    assert result["top_index"].iloc[3] == swing_bars.index[2]
    assert result["bottom"].iloc[6] == 6.0
    assert result["bottom_index"].iloc[6] == swing_bars.index[5]

    assert result["top"].notna().sum() == 1
    assert result["bottom"].notna().sum() == 2
    assert list(result["leg"]) == [0, 1, 1, -1, -1, -1, 1]


def test_pivots_alternate_and_precede_confirmation(swing_bars: pd.DataFrame) -> None:
    result = indicators.top_bottom(swing_bars, atr_period=1, reversal_atr=1.0)

    kinds: list[str] = []
    for label, row in result.iterrows():
        if pd.notna(row["top"]):
            kinds.append("top")
            assert row["top_index"] <= label
        if pd.notna(row["bottom"]):
            kinds.append("bottom")
            assert row["bottom_index"] <= label
    assert kinds == ["bottom", "top", "bottom"]


def test_prefix_invariance(swing_bars: pd.DataFrame) -> None:
    full = indicators.top_bottom(swing_bars, atr_period=1, reversal_atr=1.0)
    for cutoff in range(1, len(swing_bars) + 1):
        prefix = indicators.top_bottom(swing_bars.iloc[:cutoff], atr_period=1, reversal_atr=1.0)
        pd.testing.assert_frame_equal(prefix, full.iloc[:cutoff])


def test_output_shape_index_and_non_mutation(swing_bars: pd.DataFrame) -> None:
    original = swing_bars.copy()
    result = indicators.top_bottom(swing_bars, atr_period=1, reversal_atr=1.0)

    assert result.index is swing_bars.index
    assert list(result.columns) == ["top", "bottom", "top_index", "bottom_index", "leg"]
    assert result["leg"].dtype == np.int8
    pd.testing.assert_frame_equal(swing_bars, original)


def test_warmup_emits_no_pivots() -> None:
    bars = _bars(highs=[10, 20, 5, 25, 1], lows=[9, 19, 4, 24, 0], closes=[9.5, 19.5, 4.5, 24.5, 0.5])
    # ATR is not defined until ten bars have been seen, so this series never reaches warm-up.
    result = indicators.top_bottom(bars, atr_period=10, reversal_atr=1.0)

    assert result["top"].isna().all()
    assert result["bottom"].isna().all()
    assert (result["leg"] == 0).all()


def test_empty_frame_keeps_index() -> None:
    empty = pd.DataFrame({"high": [], "low": [], "close": []}, dtype=float)
    result = indicators.top_bottom(empty)

    assert result.empty
    assert list(result.columns) == ["top", "bottom", "top_index", "bottom_index", "leg"]


def test_missing_columns_raise(swing_bars: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        indicators.top_bottom(swing_bars.drop(columns=["low"]))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"atr_period": 0}, ValueError),
        ({"atr_period": True}, TypeError),
        ({"atr_period": 2.5}, TypeError),
        ({"reversal_atr": 0.0}, ValueError),
        ({"reversal_atr": -1.0}, ValueError),
        ({"reversal_atr": float("nan")}, ValueError),
    ],
)
def test_invalid_parameters_raise(swing_bars: pd.DataFrame, kwargs: dict, error: type[Exception]) -> None:
    with pytest.raises(error):
        indicators.top_bottom(swing_bars, **kwargs)


def test_top_bottom_is_exported() -> None:
    assert "top_bottom" in indicators.__all__
