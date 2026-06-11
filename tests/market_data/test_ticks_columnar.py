from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest

from q_backend.market_data.clients import metatrader as mt_module
from q_backend.market_data.clients.metatrader import (
    MetaTraderClient,
    _ticks_structured_to_columnar,
)
from q_backend.market_data.timezone import unix_seconds_to_brasilia_naive

_TICK_DTYPE_FULL = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
    ("last", "f8"),
    ("volume", "u8"),
    ("time_msc", "i8"),
    ("flags", "i4"),
    ("volume_real", "f8"),
]

_TICK_DTYPE_MINIMAL = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
]


def _make_ticks(count: int, base_time: int = 1_700_000_000) -> np.ndarray:
    rows = [
        (
            base_time + i,
            41.06 + i * 0.01,
            41.08 + i * 0.01,
            41.07 + i * 0.01,
            100 + i,
            (base_time + i) * 1000,
            32 if i % 2 == 0 else 64,
            0.0,
        )
        for i in range(count)
    ]
    return np.array(rows, dtype=_TICK_DTYPE_FULL)


def _make_minimal_ticks(count: int, base_time: int = 1_700_000_000) -> np.ndarray:
    rows = [
        (base_time + i, 10.0 + i, 10.1 + i)
        for i in range(count)
    ]
    return np.array(rows, dtype=_TICK_DTYPE_MINIMAL)


def test_ticks_structured_to_columnar_dtypes_and_mapping():
    ticks = _make_ticks(3)
    result = _ticks_structured_to_columnar(ticks)

    assert result["time_msc"].dtype == np.int64
    assert result["bid"].dtype == np.float64
    assert result["ask"].dtype == np.float64
    assert result["last"].dtype == np.float64
    assert result["volume"].dtype == np.float64
    assert result["flags"].dtype == np.int32

    assert list(result["time_msc"]) == [
        (1_700_000_000 + i) * 1000 for i in range(3)
    ]
    assert result["last"][1] == 41.08
    assert result["flags"][0] == 32


def test_ticks_structured_to_columnar_synthesizes_optional_fields():
    ticks = _make_minimal_ticks(2)
    result = _ticks_structured_to_columnar(ticks)

    assert len(result["time_msc"]) == 2
    assert list(result["time_msc"]) == [
        (1_700_000_000 + i) * 1000 for i in range(2)
    ]
    assert np.all(result["last"] == 0.0)
    assert np.all(result["volume"] == 0.0)
    assert np.all(result["flags"] == 0)


def test_ticks_structured_to_columnar_empty():
    result = _ticks_structured_to_columnar(np.array([], dtype=_TICK_DTYPE_FULL))
    assert len(result["time_msc"]) == 0
    assert result["bid"].dtype == np.float64


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ticks_columnar_concatenates_chunks(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7

    chunk_one = _make_ticks(2, base_time=1_700_000_000)
    chunk_two = _make_ticks(2, base_time=1_700_000_010)
    calls = {"count": 0}

    def range_side_effect(symbol, date_from, date_to, flags):
        calls["count"] += 1
        if calls["count"] == 1:
            return chunk_one
        if calls["count"] == 2:
            return chunk_two
        return np.array([], dtype=_TICK_DTYPE_FULL)

    mock_mt5.copy_ticks_range.side_effect = range_side_effect

    client = MetaTraderClient()
    client._is_initialized = True

    start = unix_seconds_to_brasilia_naive(1_700_000_000)
    end = unix_seconds_to_brasilia_naive(1_700_000_020)
    result = client.get_ticks_columnar(
        "PETR4", start, end, flags=7, use_cache=False
    )

    assert len(result["time_msc"]) == 4
    assert mock_mt5.copy_ticks_range.call_count >= 2
    assert np.all(result["time_msc"][1:] > result["time_msc"][:-1])
    assert result["time_msc"][0] == 1_700_000_000_000
    assert result["time_msc"][-1] == 1_700_000_011_000


@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ticks_columnar_empty_range(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.copy_ticks_range.return_value = np.array([], dtype=_TICK_DTYPE_FULL)

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_ticks_columnar(
        "PETR4",
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
        flags=7,
        use_cache=False,
    )

    assert len(result["time_msc"]) == 0
    assert result["bid"].dtype == np.float64


@patch("q_backend.market_data.clients.metatrader.mt5")
@patch.object(mt_module, "_MAX_TICKS", 3)
def test_get_ticks_columnar_raises_past_max_ticks(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.copy_ticks_range.return_value = _make_ticks(5)

    client = MetaTraderClient()
    client._is_initialized = True

    with pytest.raises(ValueError, match="exceeds the maximum"):
        client.get_ticks_columnar(
            "PETR4",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            flags=7,
            use_cache=False,
        )


@patch("q_backend.market_data.clients.metatrader.store_tick_cache")
@patch("q_backend.market_data.clients.metatrader.load_tick_cache")
@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ticks_columnar_use_cache_false_always_fetches(
    mock_mt5, mock_load, mock_store
):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.copy_ticks_range.return_value = _make_ticks(2)
    mock_load.return_value = {"time_msc": np.array([1], dtype=np.int64)}

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_ticks_columnar(
        "PETR4",
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
        flags=7,
        use_cache=False,
    )

    mock_load.assert_not_called()
    mock_store.assert_not_called()
    assert len(result["time_msc"]) == 2


@patch("q_backend.market_data.clients.metatrader.store_tick_cache")
@patch("q_backend.market_data.clients.metatrader.load_tick_cache")
@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ticks_columnar_uses_cache_on_hit(
    mock_mt5, mock_load, mock_store
):
    cached = _ticks_structured_to_columnar(_make_ticks(2))
    mock_load.return_value = cached

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_ticks_columnar(
        "PETR4",
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
        flags=7,
        use_cache=True,
    )

    mock_mt5.copy_ticks_range.assert_not_called()
    mock_store.assert_not_called()
    assert np.array_equal(result["time_msc"], cached["time_msc"])


@patch("q_backend.market_data.clients.metatrader.store_tick_cache")
@patch("q_backend.market_data.clients.metatrader.load_tick_cache")
@patch("q_backend.market_data.clients.metatrader.mt5")
def test_get_ticks_columnar_corrupt_cache_refetches(
    mock_mt5, mock_load, mock_store
):
    mock_load.return_value = None
    mock_mt5.symbol_select.return_value = True
    mock_mt5.COPY_TICKS_ALL = 7
    mock_mt5.copy_ticks_range.return_value = _make_ticks(2)

    client = MetaTraderClient()
    client._is_initialized = True

    result = client.get_ticks_columnar(
        "PETR4",
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
        flags=7,
        use_cache=True,
    )

    mock_store.assert_called_once()
    assert len(result["time_msc"]) == 2
