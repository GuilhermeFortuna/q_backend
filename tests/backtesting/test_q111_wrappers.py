"""Q-111 kernel wrapper tests (acceptance criteria 1–2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import q_core.indicators as qi

from q_backend.backtesting.technical_indicators import compute_log_return, compute_realized_vol_per_bar


def test_log_return_matches_kernel_index():
    close = pd.Series([100.0, 110.0, 121.0], name="close")
    expected = qi.log_return(np.asarray(close, dtype=np.float64), 1)
    actual = compute_log_return(close, 1)
    assert actual.index.equals(close.index)
    np.testing.assert_allclose(actual.to_numpy(), expected, rtol=0, atol=0)


def test_realized_vol_per_bar_matches_kernel():
    close = pd.Series([100.0, 101.0, 99.0, 102.0, 98.0])
    expected = qi.realized_vol_per_bar(np.asarray(close, dtype=np.float64), 4)
    actual = compute_realized_vol_per_bar(close, 4)
    assert actual.index.equals(close.index)
    np.testing.assert_allclose(actual.to_numpy(), expected, rtol=0, atol=0)
