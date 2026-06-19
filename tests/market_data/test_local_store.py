"""Tests for the local OHLCV parquet store (WO48)."""

from __future__ import annotations

from datetime import datetime

import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV


def _bar(day: int, year: int = 2024, close: float = 100.0) -> OHLCV:
    return OHLCV(
        time=datetime(year, 1, day),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        tick_volume=1000,
        spread=1,
        real_volume=0,
    )


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def test_write_read_round_trip_two_years(market_root):
    bars = [_bar(day, 2023, 90.0 + day) for day in range(1, 4)]
    bars += [_bar(day, 2024, 100.0 + day) for day in range(1, 6)]

    local_store.write_ohlcv("PETR4", "D1", bars)
    result = local_store.read_ohlcv(
        "PETR4",
        "D1",
        datetime(2023, 1, 2),
        datetime(2024, 1, 4),
    )

    assert len(result) == 6
    assert result[0].time == datetime(2023, 1, 2)
    assert result[-1].time == datetime(2024, 1, 4)
    assert all(result[i].time <= result[i + 1].time for i in range(len(result) - 1))
    assert result[-1].close == pytest.approx(104.0)


def test_reingest_overlapping_range_deduplicates(market_root):
    first = [_bar(day, 2024, 100.0 + day) for day in range(1, 6)]
    second = [_bar(day, 2024, 200.0 + day) for day in range(4, 8)]

    local_store.write_ohlcv("VALE3", "H1", first)
    local_store.write_ohlcv("VALE3", "H1", second)

    rows = local_store.read_ohlcv(
        "VALE3", "H1", datetime(2024, 1, 1), datetime(2024, 1, 10)
    )
    assert len(rows) == 7
    assert rows[3].close == pytest.approx(204.0)
    assert rows[0].close == pytest.approx(101.0)

    inventory = local_store.list_inventory()
    assert len(inventory) == 1
    assert inventory[0]["rows"] == 7
    assert inventory[0]["symbol"] == "VALE3"
    assert inventory[0]["timeframe"] == "H1"
    assert inventory[0]["kind"] == "bars"


def test_delete_removes_files_and_catalog(market_root):
    bars = [_bar(1), _bar(2)]
    local_store.write_ohlcv("ITUB4", "M5", bars)
    assert len(local_store.list_inventory()) == 1

    local_store.delete_ohlcv("ITUB4", "M5")
    assert local_store.list_inventory() == []
    assert not (market_root / "ohlcv" / "ITUB4" / "M5").exists()


def test_catalog_atomic_write_leaves_no_temp_file(market_root):
    local_store.write_ohlcv("PETR4", "D1", [_bar(1)])
    assert not (market_root / "catalog.json.tmp").exists()
    assert (market_root / "catalog.json").is_file()


def test_available_range_from_catalog(market_root):
    bars = [_bar(1), _bar(2), _bar(3)]
    local_store.write_ohlcv("WIN$", "D1", bars)
    available = local_store.available_range("WIN$", "D1")
    assert available is not None
    assert available.bar_count == 3
    assert available.start == datetime(2024, 1, 1)
    assert available.end == datetime(2024, 1, 3)
