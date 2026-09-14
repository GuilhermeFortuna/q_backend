"""Regression tests for edge cases recorded in tests/fixtures/lake_edge/."""

from __future__ import annotations

import builtins
from datetime import datetime
import json
from pathlib import Path
import shutil

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data import local_store
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base


@pytest.fixture
def edge_lake(tmp_path: Path, monkeypatch) -> tuple[Path, LakeCatalog]:
    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake_edge"
    dst_lake = tmp_path / "lake_edge"
    shutil.copytree(src_fixture, dst_lake)

    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(dst_lake))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)

    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()

    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)
    return dst_lake, catalog


def test_edge_cases_reproduce_baseline(edge_lake) -> None:
    lake_dir, catalog = edge_lake
    baseline_path = Path(__file__).resolve().parents[2] / "fixtures/lake_edge/expected_reads.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    for case_name, case in baseline.items():
        kind = case["kind"]
        start = datetime.fromisoformat(case["start"])
        end = datetime.fromisoformat(case["end"])

        if "raises" in case:
            exc_cls = getattr(builtins, case["raises"])
            with pytest.raises(exc_cls):
                if kind == "bars":
                    local_store.read_ohlcv(case["symbol"], case["timeframe"], start, end)
                else:
                    local_store.read_ticks_columnar(case["symbol"], start, end)
            continue

        if case_name == "bars_file_missing_after_adoption":
            file_2023 = lake_dir / "ohlcv" / "EDGE3" / "D1" / "2023.parquet"
            if file_2023.exists():
                file_2023.unlink()

        if kind == "bars":
            bars = local_store.read_ohlcv(case["symbol"], case["timeframe"], start, end)
            actual = [b.model_dump(mode="json") for b in bars]
            assert actual == case["bars"], f"Mismatch in edge case {case_name}"
        else:
            ticks = local_store.read_ticks_columnar(case["symbol"], start, end)
            for col, expected_arr in case["ticks"].items():
                np.testing.assert_array_equal(
                    ticks[col],
                    np.array(expected_arr),
                    err_msg=f"Mismatch in column {col} for edge case {case_name}",
                )
