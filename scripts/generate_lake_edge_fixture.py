#!/usr/bin/env python3
"""Generator for edge-case lake fixture and expected reads baseline (Q-020).

Writes edge partitions directly with pyarrow to create physical variance
(mixed ns/us timestamp partitions, double spread/volume with nulls, unsorted rows,
timezone-aware time column) and records expected reads with today's pandas implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data import local_store
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.storage.db.base import Base


def write_edge_lake(root: Path) -> None:
    """Write synthetic edge-case parquet lake directly with pyarrow."""
    root.mkdir(parents=True, exist_ok=True)

    # 1. EDGE3 D1: 2023.parquet (timestamp[ns], int64 spread/real_volume, sorted)
    dir_edge3_d1 = root / "ohlcv" / "EDGE3" / "D1"
    dir_edge3_d1.mkdir(parents=True, exist_ok=True)
    schema_2023 = pa.schema(
        [
            ("time", pa.timestamp("ns")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("tick_volume", pa.int64()),
            ("spread", pa.int64()),
            ("real_volume", pa.int64()),
        ]
    )
    times_2023 = [
        pd.Timestamp("2023-12-28 10:00:00").value,
        pd.Timestamp("2023-12-29 10:00:00").value,
    ]
    tbl_2023 = pa.Table.from_arrays(
        [
            pa.array(times_2023, type=pa.timestamp("ns")),
            pa.array([10.0, 10.5], type=pa.float64()),
            pa.array([11.0, 11.5], type=pa.float64()),
            pa.array([9.5, 10.0], type=pa.float64()),
            pa.array([10.5, 11.0], type=pa.float64()),
            pa.array([1000, 1100], type=pa.int64()),
            pa.array([1, 2], type=pa.int64()),
            pa.array([50000, 55000], type=pa.int64()),
        ],
        schema=schema_2023,
    )
    pq.write_table(tbl_2023, dir_edge3_d1 / "2023.parquet")

    # 2. EDGE3 D1: 2024.parquet (timestamp[us], double spread/volume with 2 nulls, unsorted)
    schema_2024 = pa.schema(
        [
            ("time", pa.timestamp("us")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("tick_volume", pa.int64()),
            ("spread", pa.float64()),
            ("real_volume", pa.float64()),
        ]
    )
    times_2024 = [
        pd.Timestamp("2024-01-04 10:00:00").value // 1000,
        pd.Timestamp("2024-01-02 10:00:00").value // 1000,
        pd.Timestamp("2024-01-05 10:00:00").value // 1000,
        pd.Timestamp("2024-01-03 10:00:00").value // 1000,
    ]
    tbl_2024 = pa.Table.from_arrays(
        [
            pa.array(times_2024, type=pa.timestamp("us")),
            pa.array([12.0, 11.0, 12.5, 11.5], type=pa.float64()),
            pa.array([13.0, 12.0, 13.5, 12.5], type=pa.float64()),
            pa.array([11.5, 10.5, 12.0, 11.0], type=pa.float64()),
            pa.array([12.5, 11.5, 13.0, 12.0], type=pa.float64()),
            pa.array([1300, 1200, 1400, 1250], type=pa.int64()),
            pa.array([None, 1.5, None, 2.5], type=pa.float64()),
            pa.array([None, 60000.0, None, 65000.0], type=pa.float64()),
        ],
        schema=schema_2024,
    )
    pq.write_table(tbl_2024, dir_edge3_d1 / "2024.parquet")

    # 3. EDGETZ H1: 2024.parquet (timestamp[us, tz=America/Sao_Paulo])
    dir_edgetz_h1 = root / "ohlcv" / "EDGETZ" / "H1"
    dir_edgetz_h1.mkdir(parents=True, exist_ok=True)
    tz_times = pd.to_datetime(
        [
            "2024-01-02 10:00:00-03:00",
            "2024-01-02 11:00:00-03:00",
            "2024-01-02 12:00:00-03:00",
        ]
    )
    schema_tz = pa.schema(
        [
            ("time", pa.timestamp("us", tz="America/Sao_Paulo")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("tick_volume", pa.int64()),
            ("spread", pa.int64()),
            ("real_volume", pa.int64()),
        ]
    )
    tbl_tz = pa.Table.from_arrays(
        [
            pa.array([t.value // 1000 for t in tz_times], type=pa.timestamp("us", tz="America/Sao_Paulo")),
            pa.array([20.0, 20.5, 21.0], type=pa.float64()),
            pa.array([21.0, 21.5, 22.0], type=pa.float64()),
            pa.array([19.5, 20.0, 20.5], type=pa.float64()),
            pa.array([20.5, 21.0, 21.5], type=pa.float64()),
            pa.array([500, 600, 700], type=pa.int64()),
            pa.array([1, 1, 1], type=pa.int64()),
            pa.array([25000, 30000, 35000], type=pa.int64()),
        ],
        schema=schema_tz,
    )
    pq.write_table(tbl_tz, dir_edgetz_h1 / "2024.parquet")

    # 4. EDGE3 ticks: 2024-12.parquet and 2025-01.parquet around midnight
    dir_ticks = root / "ticks" / "EDGE3"
    dir_ticks.mkdir(parents=True, exist_ok=True)
    schema_ticks = pa.schema(
        [
            ("time_msc", pa.int64()),
            ("bid", pa.float64()),
            ("ask", pa.float64()),
            ("last", pa.float64()),
            ("volume", pa.float64()),
            ("flags", pa.int32()),
        ]
    )
    t1 = datetime(2024, 12, 31, 23, 59, 59, 900000)
    t2 = datetime(2024, 12, 31, 23, 59, 59, 950000)
    t3 = datetime(2024, 12, 31, 23, 59, 59, 999000)
    msc_dec = [_naive_local_to_time_msc(t1), _naive_local_to_time_msc(t2), _naive_local_to_time_msc(t3)]
    tbl_dec = pa.Table.from_arrays(
        [
            pa.array(msc_dec, type=pa.int64()),
            pa.array([100.0, 100.1, 100.2], type=pa.float64()),
            pa.array([100.2, 100.3, 100.4], type=pa.float64()),
            pa.array([100.1, 100.2, 100.3], type=pa.float64()),
            pa.array([10.0, 20.0, 30.0], type=pa.float64()),
            pa.array([6, 6, 6], type=pa.int32()),
        ],
        schema=schema_ticks,
    )
    pq.write_table(tbl_dec, dir_ticks / "2024-12.parquet")

    t4 = datetime(2025, 1, 1, 0, 0, 0, 1000)
    t5 = datetime(2025, 1, 1, 0, 0, 0, 50000)
    t6 = datetime(2025, 1, 1, 0, 0, 0, 100000)
    msc_jan = [_naive_local_to_time_msc(t4), _naive_local_to_time_msc(t5), _naive_local_to_time_msc(t6)]
    tbl_jan = pa.Table.from_arrays(
        [
            pa.array(msc_jan, type=pa.int64()),
            pa.array([100.3, 100.4, 100.5], type=pa.float64()),
            pa.array([100.5, 100.6, 100.7], type=pa.float64()),
            pa.array([100.4, 100.5, 100.6], type=pa.float64()),
            pa.array([40.0, 50.0, 60.0], type=pa.float64()),
            pa.array([6, 6, 6], type=pa.int32()),
        ],
        schema=schema_ticks,
    )
    pq.write_table(tbl_jan, dir_ticks / "2025-01.parquet")


def record_expected_reads(root: Path) -> dict[str, Any]:
    """Execute edge cases with today's pandas-based local_store and record results."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    catalog = LakeCatalog(session_factory=sm, root=root)
    catalog.adopt()

    orig_root = os.environ.get("Q_MARKET_DATA_ROOT")
    orig_instance = getattr(catalog_service, "_catalog_instance", None)
    os.environ["Q_MARKET_DATA_ROOT"] = str(root)
    catalog_service._catalog_instance = catalog

    expected: dict[str, Any] = {}

    try:
        # Case 1: bars across 2023->2024 with inclusive bounds on bar times
        bars1 = local_store.read_ohlcv("EDGE3", "D1", datetime(2023, 12, 29, 10, 0, 0), datetime(2024, 1, 2, 10, 0, 0))
        expected["bars_inclusive_bounds"] = {
            "kind": "bars",
            "symbol": "EDGE3",
            "timeframe": "D1",
            "start": "2023-12-29T10:00:00",
            "end": "2024-01-02T10:00:00",
            "bars": [b.model_dump(mode="json") for b in bars1],
        }

        # Case 2: window inside 2024 matching no row
        bars2 = local_store.read_ohlcv("EDGE3", "D1", datetime(2024, 6, 1, 10, 0, 0), datetime(2024, 6, 15, 10, 0, 0))
        expected["bars_empty_range_in_partition"] = {
            "kind": "bars",
            "symbol": "EDGE3",
            "timeframe": "D1",
            "start": "2024-06-01T10:00:00",
            "end": "2024-06-15T10:00:00",
            "bars": [b.model_dump(mode="json") for b in bars2],
        }

        # Case 3: reversed window
        bars3 = local_store.read_ohlcv("EDGE3", "D1", datetime(2024, 1, 5, 10, 0, 0), datetime(2024, 1, 2, 10, 0, 0))
        expected["bars_reversed_window"] = {
            "kind": "bars",
            "symbol": "EDGE3",
            "timeframe": "D1",
            "start": "2024-01-05T10:00:00",
            "end": "2024-01-02T10:00:00",
            "bars": [b.model_dump(mode="json") for b in bars3],
        }

        # Case 4: subject with no dataset
        bars4 = local_store.read_ohlcv("UNKNOWN", "D1", datetime(2024, 1, 1, 0, 0, 0), datetime(2024, 1, 10, 0, 0, 0))
        expected["bars_unknown_subject"] = {
            "kind": "bars",
            "symbol": "UNKNOWN",
            "timeframe": "D1",
            "start": "2024-01-01T00:00:00",
            "end": "2024-01-10T00:00:00",
            "bars": [b.model_dump(mode="json") for b in bars4],
        }

        # Case 5: UTC-aware bars bounds that straddle the year boundary
        # Note: 2023-12-29 10:00 BRT is 13:00 UTC; 2024-01-03 10:00 BRT is 13:00 UTC
        start_utc = datetime(2023, 12, 29, 13, 0, 0, tzinfo=timezone.utc)
        end_utc = datetime(2024, 1, 3, 13, 0, 0, tzinfo=timezone.utc)
        bars5 = local_store.read_ohlcv("EDGE3", "D1", start_utc, end_utc)
        expected["bars_utc_aware_bounds"] = {
            "kind": "bars",
            "symbol": "EDGE3",
            "timeframe": "D1",
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "bars": [b.model_dump(mode="json") for b in bars5],
        }

        # Case 6: EDGETZ full range (timezone-aware time column)
        bars6 = local_store.read_ohlcv("EDGETZ", "H1", datetime(2024, 1, 2, 0, 0, 0), datetime(2024, 1, 2, 23, 59, 59))
        expected["bars_edgetz_full_range"] = {
            "kind": "bars",
            "symbol": "EDGETZ",
            "timeframe": "H1",
            "start": "2024-01-02T00:00:00",
            "end": "2024-01-02T23:59:59",
            "bars": [b.model_dump(mode="json") for b in bars6],
        }

        # Case 7: ticks with bounds exactly on first and last tick
        ticks7 = local_store.read_ticks_columnar(
            "EDGE3",
            datetime(2024, 12, 31, 23, 59, 59, 900000),
            datetime(2025, 1, 1, 0, 0, 0, 100000),
        )
        expected["ticks_exact_bounds"] = {
            "kind": "ticks",
            "symbol": "EDGE3",
            "start": "2024-12-31T23:59:59.900000",
            "end": "2025-01-01T00:00:00.100000",
            "ticks": {k: v.tolist() for k, v in ticks7.items()},
        }

        # Case 8: ticks with sub-millisecond bounds
        ticks8 = local_store.read_ticks_columnar(
            "EDGE3",
            datetime(2024, 12, 31, 23, 59, 59, 900500),
            datetime(2025, 1, 1, 0, 0, 0, 99500),
        )
        expected["ticks_submillisecond_bounds"] = {
            "kind": "ticks",
            "symbol": "EDGE3",
            "start": "2024-12-31T23:59:59.900500",
            "end": "2025-01-01T00:00:00.099500",
            "ticks": {k: v.tolist() for k, v in ticks8.items()},
        }

        # Case 9: UTC-aware tick bounds
        start_tick_utc = datetime(2025, 1, 1, 2, 59, 59, 900000, tzinfo=timezone.utc)
        end_tick_utc = datetime(2025, 1, 1, 3, 0, 0, 100000, tzinfo=timezone.utc)
        ticks9 = local_store.read_ticks_columnar("EDGE3", start_tick_utc, end_tick_utc)
        expected["ticks_utc_aware_bounds"] = {
            "kind": "ticks",
            "symbol": "EDGE3",
            "start": start_tick_utc.isoformat(),
            "end": end_tick_utc.isoformat(),
            "ticks": {k: v.tolist() for k, v in ticks9.items()},
        }

        # Case 10: bars with 2023 file deleted after adoption
        # Run in a separate copy to avoid mutating the main root
        with tempfile.TemporaryDirectory() as tmp_copy:
            dst = Path(tmp_copy) / "lake_edge"
            shutil.copytree(root, dst)
            eng10 = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(eng10)
            sm10 = sessionmaker(bind=eng10)
            cat10 = LakeCatalog(session_factory=sm10, root=dst)
            cat10.adopt()
            # Delete 2023.parquet
            file_2023 = dst / "ohlcv" / "EDGE3" / "D1" / "2023.parquet"
            file_2023.unlink()

            os.environ["Q_MARKET_DATA_ROOT"] = str(dst)
            catalog_service._catalog_instance = cat10
            bars10 = local_store.read_ohlcv(
                "EDGE3", "D1", datetime(2023, 12, 28, 10, 0, 0), datetime(2024, 1, 5, 10, 0, 0)
            )
            expected["bars_file_missing_after_adoption"] = {
                "kind": "bars",
                "symbol": "EDGE3",
                "timeframe": "D1",
                "start": "2023-12-28T10:00:00",
                "end": "2024-01-05T10:00:00",
                "bars": [b.model_dump(mode="json") for b in bars10],
            }

    finally:
        if orig_root is not None:
            os.environ["Q_MARKET_DATA_ROOT"] = orig_root
        else:
            os.environ.pop("Q_MARKET_DATA_ROOT", None)
        catalog_service._catalog_instance = orig_instance

    return expected


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    fixture_dir = project_root / "tests" / "fixtures" / "lake_edge"
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating edge lake in {fixture_dir}...")
    write_edge_lake(fixture_dir)

    print("Recording expected reads baseline...")
    expected = record_expected_reads(fixture_dir)

    baseline_file = fixture_dir / "expected_reads.json"
    baseline_file.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    print(f"Recorded {len(expected)} edge cases to {baseline_file}")


if __name__ == "__main__":
    main()
