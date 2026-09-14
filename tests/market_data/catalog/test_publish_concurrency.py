"""Concurrency integration tests for lock_subject and parallel publish."""

from __future__ import annotations

import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, to_manifest
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.catalog_models import Dataset
from q_backend.storage.db.engine import get_engine


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


@pytest.fixture
def pg_session_factory():
    engine = get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.integration
def test_concurrent_publishes_serialize_and_preserve_all_rows(tmp_path: Path, pg_session_factory) -> None:
    lake_root = tmp_path / "conc_lake"
    lake_root.mkdir(parents=True, exist_ok=True)

    symbol = "CONC_TEST"
    timeframe = "D1"
    subject = Subject(kind="bars", symbol=symbol, timeframe=timeframe)

    # Clean up any leftover test data
    with pg_session_factory() as session:
        session.execute(text("DELETE FROM lake_datasets WHERE symbol = :sym"), {"sym": symbol})
        session.commit()

    catalog = LakeCatalog(
        session_factory=pg_session_factory,
        root=lake_root,
        grace=timedelta(days=7),
    )

    # Publish version 1: initial 3 bars in 2025
    initial_bars = pd.DataFrame(
        [
            {
                "time": datetime(2025, 1, 2 + i, 10, 0, 0),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "tick_volume": 1000,
                "spread": 1,
                "real_volume": 5000,
            }
            for i in range(3)
        ]
    )
    v1 = catalog.publish_bars(symbol, timeframe, initial_bars)
    assert v1.version == 1

    # Prepare two disjoint sets of 2025 bars for concurrent ingest
    disjoint_a = pd.DataFrame(
        [
            {
                "time": datetime(2025, 1, 6 + i, 10, 0, 0),
                "open": 110.0 + i,
                "high": 111.0 + i,
                "low": 109.0 + i,
                "close": 110.5 + i,
                "tick_volume": 1000,
                "spread": 1,
                "real_volume": 5000,
            }
            for i in range(3)
        ]
    )
    disjoint_b = pd.DataFrame(
        [
            {
                "time": datetime(2025, 1, 10 + i, 10, 0, 0),
                "open": 120.0 + i,
                "high": 121.0 + i,
                "low": 119.0 + i,
                "close": 120.5 + i,
                "tick_volume": 1000,
                "spread": 1,
                "real_volume": 5000,
            }
            for i in range(3)
        ]
    )

    barrier = threading.Barrier(2)

    def worker(df: pd.DataFrame):
        barrier.wait()
        return catalog.publish_bars(symbol, timeframe, df)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, disjoint_a)
        f2 = executor.submit(worker, disjoint_b)
        res1 = f1.result()
        res2 = f2.result()

    versions_returned = sorted([res1.version, res2.version])
    assert versions_returned == [2, 3]

    # Verify state in DB
    with pg_session_factory() as session:
        v3 = current_dataset(session, subject)
        assert v3 is not None
        assert v3.version == 3

        # Version 3 must contain all 9 bars (3 initial + 3 disjoint_a + 3 disjoint_b)
        assert v3.row_count == 9

        manifest_v3 = to_manifest(v3)
        assert len(manifest_v3.files) == 1
        v3_file = manifest_v3.files[0]["path"]
        table = pq.read_table(lake_root / v3_file)
        assert len(table) == 9
        timestamps = sorted(table["time"].to_pylist())
        assert timestamps[0] == datetime(2025, 1, 2, 10, 0, 0)
        assert timestamps[-1] == datetime(2025, 1, 12, 10, 0, 0)

        # Cleanup test data
        session.execute(text("DELETE FROM lake_datasets WHERE symbol = :sym"), {"sym": symbol})
        session.commit()
