from datetime import datetime
from unittest.mock import patch

import numpy as np

from q_backend.market_data.clients import metatrader as mt_module
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
def test_get_ohlcv_fetches_range_in_multiple_chunks(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408

    chunk_one = np.array(
        [_rate(1_600_000_000), _rate(1_610_000_000)], dtype=_RATE_DTYPE
    )
    chunk_two = np.array(
        [_rate(1_620_000_000), _rate(1_630_000_000)], dtype=_RATE_DTYPE
    )
    calls = {"count": 0}

    def range_side_effect(symbol, timeframe, date_from, date_to):
        calls["count"] += 1
        if calls["count"] == 1:
            return chunk_one
        if calls["count"] == 2:
            return chunk_two
        return np.array([], dtype=_RATE_DTYPE)

    mock_mt5.copy_rates_range.side_effect = range_side_effect

    client = MetaTraderClient()
    client._is_initialized = True

    start = datetime.fromtimestamp(1_600_000_000)
    end = datetime.fromtimestamp(1_630_000_000)
    result = client.get_ohlcv("PETR4", "D1", start, end)

    assert len(result) == 4
    assert result[0].time == datetime.fromtimestamp(1_600_000_000)
    assert result[-1].time == datetime.fromtimestamp(1_630_000_000)
    assert mock_mt5.copy_rates_range.call_count >= 2


@patch("q_backend.market_data.clients.metatrader.mt5")
@patch.object(mt_module, "_MAX_OHLCV_BARS", 3)
def test_get_ohlcv_respects_max_bar_cap(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408
    mock_mt5.copy_rates_range.return_value = np.array(
        [_rate(1), _rate(2), _rate(3), _rate(4), _rate(5)],
        dtype=_RATE_DTYPE,
    )

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_ohlcv(
        "PETR4",
        "D1",
        datetime.fromtimestamp(1),
        datetime.fromtimestamp(5),
    )

    assert len(result) == 3


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ohlcv_accepts_timezone_aware_range(mock_mt5):
    from datetime import timezone

    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408
    mock_mt5.copy_rates_range.return_value = np.array(
        [_rate(1_600_000_000), _rate(1_610_000_000)],
        dtype=_RATE_DTYPE,
    )

    client = MetaTraderClient()
    client._is_initialized = True

    start = datetime.fromtimestamp(1_600_000_000, tz=timezone.utc)
    end = datetime.fromtimestamp(1_610_000_000, tz=timezone.utc)
    result = client.get_ohlcv("PETR4", "D1", start, end)

    assert len(result) == 2
    assert mock_mt5.copy_rates_range.call_count == 1


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ohlcv_returns_empty_when_no_rates(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.TIMEFRAME_D1 = 16408
    mock_mt5.copy_rates_range.return_value = None
    mock_mt5.last_error.return_value = (1, "no data")

    client = MetaTraderClient()
    client._is_initialized = True

    assert (
        client.get_ohlcv(
            "PETR4",
            "D1",
            datetime.fromtimestamp(1_600_000_000),
            datetime.fromtimestamp(1_630_000_000),
        )
        == []
    )
