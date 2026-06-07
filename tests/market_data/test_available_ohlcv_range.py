from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest

from q_backend.market_data.clients.metatrader import MetaTraderClient

_RATE_DTYPE = [
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
]


def _rate(time: int) -> tuple:
    return (time, 40.0, 41.0, 39.0, 40.5, 1000, 1, 0)


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_available_ohlcv_range_uses_copy_rates_from_for_earliest(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_H1 = 16385
    mock_mt5.copy_rates_from.return_value = np.array([_rate(1_577_836_800)], dtype=_RATE_DTYPE)
    mock_mt5.copy_rates_from_pos.return_value = np.array([_rate(1_704_067_200)], dtype=_RATE_DTYPE)
    mock_mt5.copy_rates_range.return_value = np.array(
        [_rate(1_577_836_800), _rate(1_704_067_200)],
        dtype=_RATE_DTYPE,
    )

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_available_ohlcv_range("CCM$", "H1")

    assert result is not None
    assert result.start == datetime.fromtimestamp(1_577_836_800)
    assert result.end == datetime.fromtimestamp(1_704_067_200)
    assert result.bar_count == 2
    mock_mt5.copy_rates_from.assert_called_once()
    mock_mt5.copy_rates_from_pos.assert_called_once_with("CCM$", 16385, 0, 1)


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_available_ohlcv_range_scans_range_when_copy_rates_from_empty(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408
    mock_mt5.copy_rates_from.return_value = None
    mock_mt5.copy_rates_from_pos.return_value = np.array([_rate(1_704_067_200)], dtype=_RATE_DTYPE)

    older_chunk = np.array([_rate(1_577_836_800), _rate(1_600_000_000)], dtype=_RATE_DTYPE)

    def range_side_effect(symbol, timeframe, date_from, date_to):
        if date_from.year < 2020:
            return older_chunk
        return np.array([], dtype=_RATE_DTYPE)

    mock_mt5.copy_rates_range.side_effect = range_side_effect

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_available_ohlcv_range("PETR4", "D1")

    assert result is not None
    assert result.start == datetime.fromtimestamp(1_577_836_800)
    assert mock_mt5.copy_rates_range.call_count >= 1


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_available_ohlcv_range_returns_none_when_no_bars(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408
    mock_mt5.copy_rates_from.return_value = None
    mock_mt5.copy_rates_from_pos.return_value = None
    mock_mt5.copy_rates_range.return_value = None
    mock_mt5.last_error.return_value = (1, "no data")

    client = MetaTraderClient()
    client._is_initialized = True

    assert client.get_available_ohlcv_range("PETR4", "D1") is None


def test_get_available_ohlcv_range_rejects_invalid_timeframe():
    client = MetaTraderClient()
    client._is_initialized = True

    with pytest.raises(ValueError):
        client.get_available_ohlcv_range("PETR4", "INVALID")
