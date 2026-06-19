"""Tests for the local tick parquet store (WO50)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pyarrow.parquet as pq
import pytest

from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def _synthetic_ticks_two_months() -> dict[str, np.ndarray]:
    jan_base = datetime(2024, 1, 15, 10, 0, 0)
    feb_base = datetime(2024, 2, 10, 10, 0, 0)
    time_msc = [
        _naive_local_to_time_msc(jan_base + timedelta(seconds=i)) for i in range(5)
    ] + [
        _naive_local_to_time_msc(feb_base + timedelta(seconds=i)) for i in range(5)
    ]
    n = len(time_msc)
    prices = 100.0 + np.arange(n, dtype=np.float64) * 0.1
    return {
        "time_msc": np.array(time_msc, dtype=np.int64),
        "bid": prices - 0.01,
        "ask": prices + 0.01,
        "last": prices,
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.zeros(n, dtype=np.int32),
    }


def test_write_read_round_trip_two_months(market_root):
    arrays = _synthetic_ticks_two_months()
    local_store.write_ticks("PETR4", arrays)

    result = local_store.read_ticks_columnar(
        "PETR4",
        datetime(2024, 1, 15, 10, 0, 1),
        datetime(2024, 1, 15, 10, 0, 3),
    )

    assert len(result["time_msc"]) == 3
    assert np.all(result["time_msc"][1:] > result["time_msc"][:-1])
    assert result["bid"][0] == pytest.approx(100.09)
    assert (market_root / "ticks" / "PETR4" / "2024-01.parquet").is_file()
    assert (market_root / "ticks" / "PETR4" / "2024-02.parquet").is_file()


def test_reingest_overlapping_range_deduplicates(market_root):
    arrays = _synthetic_ticks_two_months()
    overlap = dict(arrays)
    overlap["bid"] = overlap["bid"] + 10.0
    overlap["ask"] = overlap["ask"] + 10.0
    overlap["last"] = overlap["last"] + 10.0

    local_store.write_ticks("VALE3", arrays)
    local_store.write_ticks("VALE3", overlap)

    result = local_store.read_ticks_columnar(
        "VALE3", datetime(2024, 1, 1), datetime(2024, 3, 1)
    )
    assert len(result["time_msc"]) == 10
    assert result["bid"][0] == pytest.approx(109.99)

    inventory = local_store.list_inventory()
    tick_rows = [row for row in inventory if row.get("kind") == "ticks"]
    assert len(tick_rows) == 1
    assert tick_rows[0]["rows"] == 10
    assert tick_rows[0]["symbol"] == "VALE3"


def test_persisted_schema_matches_columnar_contract(market_root):
    local_store.write_ticks("WIN$", _synthetic_ticks_two_months())
    path = market_root / "ticks" / "WIN$" / "2024-01.parquet"
    table = pq.read_table(path)
    assert list(table.column_names) == list(COLUMNAR_TICK_KEYS)
    for col in COLUMNAR_TICK_KEYS:
        arr = table[col].to_numpy(zero_copy_only=False)
        assert arr.dtype == _TICK_DTYPE_MAP[col]


def test_delete_ticks_removes_files_and_catalog(market_root):
    local_store.write_ticks("ITUB4", _synthetic_ticks_two_months())
    assert len(local_store.list_inventory()) == 1

    local_store.delete_ticks("ITUB4")
    assert local_store.list_inventory() == []
    assert not (market_root / "ticks" / "ITUB4").exists()


def test_tick_available_range_from_catalog(market_root):
    local_store.write_ticks("PETR4", _synthetic_ticks_two_months())
    available = local_store.tick_available_range("PETR4")
    assert available is not None
    assert available["kind"] == "ticks"
    assert available["rows"] == 10
