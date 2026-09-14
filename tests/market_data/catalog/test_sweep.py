"""Unit tests for catalog dataset sweeping and reference-aware deletion."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, get_dataset
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base


@pytest.fixture
def lake_copy(tmp_path: Path) -> Path:
    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)
    return dst_lake


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_sweep_respects_grace_and_preserves_shared_and_unreferenced_files(lake_copy: Path, session_factory) -> None:
    now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    current_time = now
    catalog = LakeCatalog(
        session_factory=session_factory,
        root=lake_copy,
        grace=timedelta(days=7),
        clock=lambda: current_time,
    )
    catalog.adopt()

    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    with session_factory() as session:
        v1 = current_dataset(session, subject)
        assert v1 is not None
        v1_id = v1.dataset_id
        v1_2024_path = next(f.path for f in v1.files if "2024" in f.path)
        v1_2025_path = next(f.path for f in v1.files if "2025" in f.path)

    # Publish version 2 (tombstoning v1 with grace = now + 7 days)
    new_bars = pd.DataFrame(
        [
            {
                "time": datetime(2025, 1, 20, 10, 0, 0),
                "open": 45.0,
                "high": 46.0,
                "low": 44.5,
                "close": 45.5,
                "tick_volume": 15000,
                "spread": 1,
                "real_volume": 750000,
            }
        ]
    )
    v2 = catalog.publish_bars("PETR4", "D1", new_bars)
    v2_2025_path = next(f.path for f in v2.files if "2025" in f.path)

    # Place an unreferenced content-addressed file in the lake
    unref_rel_path = "ohlcv/PETR4/D1/2025.0123456789abcdef.parquet"
    unref_full_path = lake_copy / unref_rel_path
    unref_full_path.write_bytes(b"dummy unreferenced parquet content")

    # 1. Sweep at now + 6 days: grace has NOT passed
    current_time = now + timedelta(days=6)
    report_early = catalog.sweep()
    assert report_early.datasets_deleted == 0
    assert report_early.files_deleted == 0
    assert report_early.bytes_freed == 0
    assert unref_rel_path in report_early.unreferenced_files
    assert unref_full_path.is_file()  # Unreferenced file was reported, NOT deleted
    assert (lake_copy / v1_2025_path).is_file()

    # 2. Sweep at now + 8 days: grace HAS passed for v1
    current_time = now + timedelta(days=8)
    report_due = catalog.sweep()
    assert report_due.datasets_deleted == 1
    assert report_due.files_deleted == 1  # Only v1's 2025 file
    assert report_due.bytes_freed > 0
    assert unref_rel_path in report_due.unreferenced_files

    # The tombstoned 2025 file is gone
    assert not (lake_copy / v1_2025_path).exists()

    # The shared 2024 file is STILL present
    assert (lake_copy / v1_2024_path).is_file()

    # v2's 2025 file is STILL present
    assert (lake_copy / v2_2025_path).is_file()

    # The unreferenced file is STILL present
    assert unref_full_path.is_file()

    # In DB, v1 is now state="deleted"
    with session_factory() as session:
        v1_reloaded = get_dataset(session, v1_id)
        assert v1_reloaded is not None
        assert v1_reloaded.state == "deleted"

        v2_reloaded = current_dataset(session, subject)
        assert v2_reloaded is not None
        assert v2_reloaded.state == "published"
