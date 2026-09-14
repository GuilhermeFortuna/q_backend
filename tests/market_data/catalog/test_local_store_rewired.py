"""Tests verifying local_store rewiring to the database catalog."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.api.schemas.storage import StorageInventoryItem
from q_backend.market_data import local_store
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base


@pytest.fixture
def rewired_lake(tmp_path: Path, monkeypatch) -> tuple[Path, sessionmaker, LakeCatalog]:
    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)

    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(dst_lake))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)

    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()

    # Point the catalog singleton in service to our test catalog
    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)

    return dst_lake, sm, catalog


def test_reads_match_expected_baseline(rewired_lake) -> None:
    lake_dir, _, _ = rewired_lake
    baseline_path = Path(__file__).resolve().parents[2] / "fixtures/lake/expected_reads.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    # Range 1: PETR4 D1
    b_petr4 = baseline["petr4_d1"]
    bars_petr4 = local_store.read_ohlcv(
        b_petr4["symbol"],
        b_petr4["timeframe"],
        datetime.fromisoformat(b_petr4["start"]),
        datetime.fromisoformat(b_petr4["end"]),
    )
    assert [b.model_dump(mode="json") for b in bars_petr4] == b_petr4["bars"]

    # Range 2: WIN$N M15
    b_winn = baseline["winn_m15"]
    bars_winn = local_store.read_ohlcv(
        b_winn["symbol"],
        b_winn["timeframe"],
        datetime.fromisoformat(b_winn["start"]),
        datetime.fromisoformat(b_winn["end"]),
    )
    assert [b.model_dump(mode="json") for b in bars_winn] == b_winn["bars"]

    # Range 3: WIN$N ticks
    b_ticks = baseline["winn_ticks"]
    ticks = local_store.read_ticks_columnar(
        b_ticks["symbol"],
        datetime.fromisoformat(b_ticks["start"]),
        datetime.fromisoformat(b_ticks["end"]),
    )
    for col, expected_arr in b_ticks["ticks"].items():
        np.testing.assert_array_equal(ticks[col], np.array(expected_arr))


def test_reads_succeed_without_directory_listing(rewired_lake) -> None:
    def forbidden_listing(*args, **kwargs):
        raise AssertionError("Directory listing is forbidden on the read path")

    with (
        patch.object(Path, "glob", forbidden_listing),
        patch.object(Path, "iterdir", forbidden_listing),
        patch("os.listdir", forbidden_listing),
    ):
        bars = local_store.read_ohlcv("PETR4", "D1", datetime(2024, 12, 15), datetime(2025, 1, 15))
        assert len(bars) > 0

        ticks = local_store.read_ticks_columnar("WIN$N", datetime(2025, 1, 1), datetime(2025, 2, 28))
        assert len(ticks["time_msc"]) > 0


def test_delete_ohlcv_tombstones_and_leaves_files(rewired_lake) -> None:
    lake_dir, sm, _ = rewired_lake
    series_dir = lake_dir / "ohlcv" / "PETR4" / "D1"
    files_before = set(series_dir.glob("*.parquet"))
    assert len(files_before) == 2

    local_store.delete_ohlcv("PETR4", "D1")

    # Files must still exist on disk (tombstoned, not removed)
    files_after = set(series_dir.glob("*.parquet"))
    assert files_after == files_before

    # Database has no current published dataset
    with sm() as session:
        current = current_dataset(session, Subject(kind="bars", symbol="PETR4", timeframe="D1"))
        assert current is None

    # Inventory no longer lists PETR4 D1
    inventory = local_store.list_inventory()
    assert not any(i["symbol"] == "PETR4" and i.get("timeframe") == "D1" for i in inventory)


def test_list_inventory_validates_as_storage_inventory_items(rewired_lake) -> None:
    inventory = local_store.list_inventory()
    assert len(inventory) == 3

    for item in inventory:
        validated = StorageInventoryItem(**item)
        assert validated.rows > 0
        assert validated.bytes > 0
