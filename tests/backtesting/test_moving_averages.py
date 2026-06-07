import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.moving_averages import compute_ma, normalize_ma_type


def test_normalize_ma_type_accepts_supported_values():
    assert normalize_ma_type("SMA") == "sma"
    assert normalize_ma_type("ema") == "ema"
    assert normalize_ma_type(" HMA ") == "hma"


def test_normalize_ma_type_rejects_unknown_values():
    with pytest.raises(ValueError, match="Invalid MA type"):
        normalize_ma_type("tema")


def test_compute_ma_types_produce_different_values_for_ema():
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    sma = compute_ma(closes, 3, "sma")
    ema = compute_ma(closes, 3, "ema")

    assert not np.isnan(sma.iloc[2])
    assert not np.isnan(ema.iloc[2])
    assert sma.iloc[-1] != ema.iloc[-1]


def test_compute_ma_wma_smma_and_hma_return_series():
    closes = pd.Series(np.linspace(100.0, 110.0, 20))

    for ma_type in ("wma", "smma", "hma"):
        result = compute_ma(closes, 5, ma_type)
        assert len(result) == len(closes)
        assert result.notna().sum() > 0
