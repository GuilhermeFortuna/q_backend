"""Unit and integration tests for immutable dataset publication."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, get_dataset, to_manifest
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


def test_publish_bars_yields_version_2_and_tombstones_v1(lake_copy: Path, session_factory) -> None:
    now_v1 = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    catalog = LakeCatalog(
        session_factory=session_factory,
        root=lake_copy,
        grace=timedelta(days=7),
        clock=lambda: now_v1,
    )
    catalog.adopt()

    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    with session_factory() as session:
        v1 = current_dataset(session, subject)
        assert v1 is not None
        v1_id = v1.dataset_id
        v1_manifest = to_manifest(v1)

    v1_2024_path = next(f["path"] for f in v1_manifest.files if "2024" in f["path"])
    v1_2025_path = next(f["path"] for f in v1_manifest.files if "2025" in f["path"])
    v1_2025_bytes_before = (lake_copy / v1_2025_path).read_bytes()
    v1_2025_sha_before = hashlib.sha256(v1_2025_bytes_before).hexdigest()

    # Open the v1 2025 file before the publish
    v1_2025_opened = open(lake_copy / v1_2025_path, "rb")

    # Publish new bars only in 2025
    now_v2 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    catalog.clock = lambda: now_v2

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

    # After publish, read from the previously opened file
    v1_2025_opened.seek(0)
    bytes_from_opened = v1_2025_opened.read()
    v1_2025_opened.close()
    assert bytes_from_opened == v1_2025_bytes_before

    # Verify version 1 2025 file on disk was not modified
    assert hashlib.sha256((lake_copy / v1_2025_path).read_bytes()).hexdigest() == v1_2025_sha_before

    # Check v2 manifest
    v2_manifest = to_manifest(v2)
    assert v2.version == 2
    assert v2.supersedes == v1_id
    assert v2_manifest.supersedes == str(v1_id)

    # 2024 path is identical in both manifests
    v2_2024_path = next(f["path"] for f in v2_manifest.files if "2024" in f["path"])
    assert v2_2024_path == v1_2024_path

    # 2025 path differs (content-addressed)
    v2_2025_path = next(f["path"] for f in v2_manifest.files if "2025" in f["path"])
    assert v2_2025_path != v1_2025_path
    assert (lake_copy / v2_2025_path).is_file()

    # Verify version 1 is tombstoned with deadline = now_v2 + 7 days
    with session_factory() as session:
        v1_reloaded = get_dataset(session, v1_id)
        assert v1_reloaded.deletable_after is not None
        deletable_after_utc = (
            v1_reloaded.deletable_after
            if v1_reloaded.deletable_after.tzinfo is not None
            else v1_reloaded.deletable_after.replace(tzinfo=timezone.utc)
        )
        assert deletable_after_utc == now_v2 + timedelta(days=7)


def test_publish_interrupted_before_commit_leaves_v1_and_retry_publishes_once(lake_copy: Path, session_factory) -> None:
    now_v1 = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    catalog = LakeCatalog(
        session_factory=session_factory,
        root=lake_copy,
        grace=timedelta(days=7),
        clock=lambda: now_v1,
    )
    catalog.adopt()

    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
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

    # Patch session.commit to simulate crash right after file write
    original_commit = Session.commit

    def fail_commit(self):
        raise RuntimeError("Simulated DB crash during commit")

    with patch.object(Session, "commit", fail_commit):
        with pytest.raises(RuntimeError, match="Simulated DB crash"):
            catalog.publish_bars("PETR4", "D1", new_bars)

    # Current dataset in DB is still version 1
    with session_factory() as session:
        current = current_dataset(session, subject)
        assert current is not None
        assert current.version == 1

    # Retry the exact same publish
    v2 = catalog.publish_bars("PETR4", "D1", new_bars)
    assert v2.version == 2

    # Check target directory: only one content-addressed 2025 file exists (plus legacy 2024.parquet, 2025.parquet)
    series_dir = lake_copy / "ohlcv" / "PETR4" / "D1"
    all_2025_files = list(series_dir.glob("2025*.parquet"))
    # One legacy 2025.parquet, and exactly one 2025.<sha16>.parquet
    assert len(all_2025_files) == 2
