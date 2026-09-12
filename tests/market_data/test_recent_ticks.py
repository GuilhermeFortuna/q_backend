from datetime import datetime
from unittest.mock import patch

import numpy as np

from q_backend.market_data.clients.metatrader import MetaTraderClient

_TICK_DTYPE = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
    ("last", "f8"),
    ("volume", "u8"),
    ("time_msc", "i8"),
    ("flags", "i4"),
    ("volume_real", "f8"),
]


def _make_ticks(count: int) -> np.ndarray:
    base = int(datetime(2026, 6, 9, 14, 32, 0).timestamp())
    rows = [
        (
            base + i,
            41.06 + i * 0.01,
            41.08 + i * 0.01,
            41.07 + i * 0.01,
            100 + i,
            (base + i) * 1000,
            32 if i % 2 == 0 else 64,
            0.0,
        )
        for i in range(count)
    ]
    return np.array(rows, dtype=_TICK_DTYPE)


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_recent_ticks_uses_copy_ticks_range(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    ticks = _make_ticks(3)
    mock_mt5.copy_ticks_range.return_value = ticks

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_recent_ticks("PETR4", limit=2)

    # The first window already returns >= limit ticks, so only one range call.
    assert mock_mt5.copy_ticks_range.call_count == 1
    range_args = mock_mt5.copy_ticks_range.call_args[0]
    assert range_args[0] == "PETR4"
    # Range is (symbol, start, end, flags) and must be ascending in time.
    assert range_args[1] < range_args[2]

    # Only the newest `limit` ticks are returned, newest last.
    assert len(result) == 2
    assert result[0].last == 41.08  # tick index 1
    assert result[1].last == 41.09  # tick index 2 (newest)
    assert result[1].flags == 32


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_recent_ticks_widens_window_when_too_few(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7

    # First window: empty. Second: one tick. Third: enough ticks -> stop.
    mock_mt5.copy_ticks_range.side_effect = [
        np.array([], dtype=_TICK_DTYPE),
        _make_ticks(1),
        _make_ticks(5),
    ]

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_recent_ticks("WIN$", limit=3)

    # Stops as soon as a window yields >= limit ticks (3rd call), not the 4th.
    assert mock_mt5.copy_ticks_range.call_count == 3

    # Windows must widen: each call's look-back span grows.
    spans = [
        end - start for (_, start, end, _flags) in (call.args for call in mock_mt5.copy_ticks_range.call_args_list)
    ]
    assert spans[0] < spans[1] < spans[2]

    assert len(result) == 3


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_recent_ticks_returns_empty_when_no_data(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.last_error.return_value = (1, "no ticks")
    mock_mt5.copy_ticks_range.return_value = np.array([], dtype=_TICK_DTYPE)

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_recent_ticks("PETR4", limit=10)

    # Every window exhausted with no data.
    assert mock_mt5.copy_ticks_range.call_count == 4
    assert result == []
