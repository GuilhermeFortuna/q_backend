"""Integration tests for local_store backed by DuckDB lake_query (Q-020)."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
import json
import multiprocessing
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data import local_store
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.market_data.lake_query import query_cursor
from q_backend.storage.db.base import Base


@pytest.fixture
def baseline_data() -> dict:
    baseline_path = Path(__file__).resolve().parents[2] / "fixtures/lake/expected_reads.json"
    return json.loads(baseline_path.read_text(encoding="utf-8"))


@pytest.fixture
def test_lake(tmp_path: Path, monkeypatch) -> tuple[Path, LakeCatalog]:
    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)

    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(dst_lake))

    db_path = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)

    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()

    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)
    return dst_lake, catalog


def test_reads_succeed_without_pyarrow_parquet_read_table(test_lake, baseline_data) -> None:
    def forbidden_read_table(*args, **kwargs):
        raise AssertionError("pyarrow.parquet.read_table should not be called")

    with patch("pyarrow.parquet.read_table", forbidden_read_table):
        # 1. PETR4 D1
        b_petr4 = baseline_data["petr4_d1"]
        bars_petr4 = local_store.read_ohlcv(
            b_petr4["symbol"],
            b_petr4["timeframe"],
            datetime.fromisoformat(b_petr4["start"]),
            datetime.fromisoformat(b_petr4["end"]),
        )
        assert [b.model_dump(mode="json") for b in bars_petr4] == b_petr4["bars"]

        # 2. WIN$N M15
        b_winn = baseline_data["winn_m15"]
        bars_winn = local_store.read_ohlcv(
            b_winn["symbol"],
            b_winn["timeframe"],
            datetime.fromisoformat(b_winn["start"]),
            datetime.fromisoformat(b_winn["end"]),
        )
        assert [b.model_dump(mode="json") for b in bars_winn] == b_winn["bars"]

        # 3. WIN$N ticks
        b_ticks = baseline_data["winn_ticks"]
        ticks = local_store.read_ticks_columnar(
            b_ticks["symbol"],
            datetime.fromisoformat(b_ticks["start"]),
            datetime.fromisoformat(b_ticks["end"]),
        )
        for col, expected_arr in b_ticks["ticks"].items():
            np.testing.assert_array_equal(ticks[col], np.array(expected_arr))


def test_reads_succeed_on_unlistable_directories_and_pattern_fails(tmp_path: Path, monkeypatch, baseline_data) -> None:
    if os.geteuid() == 0:
        pytest.skip("Root ignores directory permissions")

    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake"
    dst_lake = tmp_path / "lake_unlistable"
    shutil.copytree(src_fixture, dst_lake)

    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(dst_lake))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)

    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()

    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)

    all_dirs = [d for d in dst_lake.rglob("*") if d.is_dir()]
    all_dirs.append(dst_lake)

    try:
        # Make directories unlistable (mode 0o311: execute & write, no read)
        for d in all_dirs:
            d.chmod(0o311)

        # Reads must succeed over catalog-listed files
        b_petr4 = baseline_data["petr4_d1"]
        bars_petr4 = local_store.read_ohlcv(
            b_petr4["symbol"],
            b_petr4["timeframe"],
            datetime.fromisoformat(b_petr4["start"]),
            datetime.fromisoformat(b_petr4["end"]),
        )
        assert [b.model_dump(mode="json") for b in bars_petr4] == b_petr4["bars"]

        b_ticks = baseline_data["winn_ticks"]
        ticks = local_store.read_ticks_columnar(
            b_ticks["symbol"],
            datetime.fromisoformat(b_ticks["start"]),
            datetime.fromisoformat(b_ticks["end"]),
        )
        for col, expected_arr in b_ticks["ticks"].items():
            np.testing.assert_array_equal(ticks[col], np.array(expected_arr))

        # Negative control: pattern on unlistable directory returns no files or raises
        cur = query_cursor()
        pattern = str(dst_lake / "ohlcv" / "PETR4" / "D1" / "*.parquet")
        pattern_failed = False
        try:
            res = cur.execute("SELECT * FROM read_parquet(?)", [pattern]).fetchall()
            pattern_failed = len(res) == 0
        except Exception:  # noqa: BLE001
            pattern_failed = True
        assert pattern_failed, "Pattern scan on unlistable directory should fail or return 0 rows"

    finally:
        # Restore permissions for clean pytest teardown
        for d in all_dirs:
            d.chmod(0o755)


def test_concurrent_threads_read_baseline(test_lake, baseline_data) -> None:
    def _worker():
        for _ in range(20):
            # PETR4 D1
            b_petr4 = baseline_data["petr4_d1"]
            bars_petr4 = local_store.read_ohlcv(
                b_petr4["symbol"],
                b_petr4["timeframe"],
                datetime.fromisoformat(b_petr4["start"]),
                datetime.fromisoformat(b_petr4["end"]),
            )
            assert len(bars_petr4) == len(b_petr4["bars"])

            # WIN$N M15
            b_winn = baseline_data["winn_m15"]
            bars_winn = local_store.read_ohlcv(
                b_winn["symbol"],
                b_winn["timeframe"],
                datetime.fromisoformat(b_winn["start"]),
                datetime.fromisoformat(b_winn["end"]),
            )
            assert len(bars_winn) == len(b_winn["bars"])

            # WIN$N ticks
            b_ticks = baseline_data["winn_ticks"]
            ticks = local_store.read_ticks_columnar(
                b_ticks["symbol"],
                datetime.fromisoformat(b_ticks["start"]),
                datetime.fromisoformat(b_ticks["end"]),
            )
            assert len(ticks["time_msc"]) == len(b_ticks["ticks"]["time_msc"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_worker) for _ in range(8)]
        for f in futures:
            f.result()


def test_forked_child_reads_baseline(test_lake, baseline_data) -> None:
    # Parent reads first
    b_ticks = baseline_data["winn_ticks"]
    parent_ticks = local_store.read_ticks_columnar(
        b_ticks["symbol"],
        datetime.fromisoformat(b_ticks["start"]),
        datetime.fromisoformat(b_ticks["end"]),
    )
    assert len(parent_ticks["time_msc"]) > 0

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()

    def _child(q):
        try:
            ticks = local_store.read_ticks_columnar(
                b_ticks["symbol"],
                datetime.fromisoformat(b_ticks["start"]),
                datetime.fromisoformat(b_ticks["end"]),
            )
            # Send serialized list of time_msc
            q.put({"status": "ok", "time_msc": ticks["time_msc"].tolist()})
        except Exception as exc:  # noqa: BLE001
            q.put({"status": "error", "error": str(exc)})

    p = ctx.Process(target=_child, args=(queue,))
    p.start()
    res = queue.get(timeout=10)
    p.join(timeout=10)

    assert res["status"] == "ok", f"Child failed: {res.get('error')}"
    assert res["time_msc"] == b_ticks["ticks"]["time_msc"]
