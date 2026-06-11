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


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_recent_ticks_uses_copy_ticks_from(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.copy_ticks_from.return_value = np.array(
        [
            (
                int(datetime(2026, 6, 9, 14, 32, 10).timestamp()),
                41.06,
                41.08,
                41.07,
                100,
                1_749_486_730_000,
                32,
                0.0,
            ),
            (
                int(datetime(2026, 6, 9, 14, 32, 11).timestamp()),
                41.07,
                41.09,
                41.08,
                300,
                1_749_486_731_000,
                64,
                0.0,
            ),
        ],
        dtype=_TICK_DTYPE,
    )

    client = MetaTraderClient()
    client._is_initialized = True

    ticks = client.get_recent_ticks("PETR4", limit=2)

    assert len(ticks) == 2
    assert ticks[0].last == 41.07
    assert ticks[1].flags == 64
    mock_mt5.copy_ticks_from.assert_called_once()
    call_args = mock_mt5.copy_ticks_from.call_args[0]
    assert call_args[0] == "PETR4"
    assert call_args[2] == -2
