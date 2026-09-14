"""Unit tests for immutable partition writes and schema derivation."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from q_backend.market_data.catalog.partitions import (
    Subject,
    WrittenFile,
    describe_existing_partition,
    lake_arrow_schema,
    write_partition_immutable,
)


@pytest.fixture
def lake_dir(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def sample_bars_table() -> pa.Table:
    return pa.Table.from_pydict(
        {
            "time": [
                datetime(2025, 1, 2, 9, 0, 0),
                datetime(2025, 1, 2, 9, 15, 0),
            ],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "tick_volume": [1000, 1500],
            "spread": [1, 1],
            "real_volume": [5000, 7500],
        }
    )


def test_write_partition_immutable_creates_content_addressed_file(lake_dir: Path, sample_bars_table: pa.Table) -> None:
    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    written = write_partition_immutable(lake_dir, subject, "2025", sample_bars_table)

    file_path = lake_dir / written.path
    assert file_path.is_file()

    # Name contains first 16 hex characters of sha256
    expected_hex16 = written.checksum[:16]
    assert f"2025.{expected_hex16}.parquet" == file_path.name

    # Size matches disk stat
    assert written.size_bytes == file_path.stat().st_size

    # Checksum matches fresh sha256 of file bytes
    file_bytes = file_path.read_bytes()
    assert written.checksum == hashlib.sha256(file_bytes).hexdigest()
    assert written.rows == 2


def test_writing_same_table_twice_returns_same_path_and_leaves_one_file(
    lake_dir: Path, sample_bars_table: pa.Table
) -> None:
    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    first = write_partition_immutable(lake_dir, subject, "2025", sample_bars_table)
    second = write_partition_immutable(lake_dir, subject, "2025", sample_bars_table)

    assert first.path == second.path
    assert first.checksum == second.checksum

    parent_dir = (lake_dir / first.path).parent
    parquet_files = list(parent_dir.glob("*.parquet"))
    assert len(parquet_files) == 1


def test_os_replace_failure_cleans_temp_file_and_leaves_no_target(lake_dir: Path, sample_bars_table: pa.Table) -> None:
    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    target_dir = lake_dir / "ohlcv" / "PETR4" / "D1"

    with patch("os.replace", side_effect=OSError("Disk failure")), pytest.raises(OSError, match="Disk failure"):
        write_partition_immutable(lake_dir, subject, "2025", sample_bars_table)

    # No parquet files or temp files remain in target dir
    if target_dir.exists():
        files = list(target_dir.iterdir())
        assert len(files) == 0


def test_lake_arrow_schema_on_bars_fixture_has_naive_wallclock_tz() -> None:
    fixture_bars_path = Path(__file__).resolve().parents[2] / "fixtures/lake/ohlcv/PETR4/D1/2024.parquet"
    table = pq.read_table(fixture_bars_path)
    schema_dict = lake_arrow_schema(table.schema, name="bars")

    assert schema_dict["name"] == "bars"
    time_field = next(f for f in schema_dict["fields"] if f["name"] == "time")
    assert time_field["tz"] == "naive-wallclock-America/Sao_Paulo"
    assert time_field["type"] in ("timestamp[us]", "timestamp[ns]")


def test_describe_existing_partition(lake_dir: Path, sample_bars_table: pa.Table) -> None:
    subject = Subject(kind="bars", symbol="PETR4", timeframe="D1")
    written = write_partition_immutable(lake_dir, subject, "2025", sample_bars_table)

    described = describe_existing_partition(lake_dir, written.path)
    assert described.path == written.path
    assert described.size_bytes == written.size_bytes
    assert described.checksum == written.checksum
    assert described.rows == written.rows
    assert described.partition_start == written.partition_start
    assert described.partition_end == written.partition_end
