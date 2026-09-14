"""Tests for scripts/lake_read_digest.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP


def _load_script():
    script_path = Path(__file__).resolve().parents[3] / "scripts/lake_read_digest.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    spec = importlib.util.spec_from_file_location("lake_read_digest", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["lake_read_digest"] = module
    spec.loader.exec_module(module)
    return module


def test_planned_reads() -> None:
    mod = _load_script()
    fixture_catalog = Path(__file__).resolve().parents[2] / "fixtures/lake/catalog.json"
    inventory = json.loads(fixture_catalog.read_text(encoding="utf-8"))

    specs = mod.planned_reads(inventory)
    # 3 datasets × (full range + last month) = 6 reads
    assert len(specs) == 6

    # Verify kinds and symbols
    assert [s.symbol for s in specs] == ["PETR4", "PETR4", "WIN$N", "WIN$N", "WIN$N", "WIN$N"]
    assert [s.kind for s in specs] == ["bars", "bars", "bars", "bars", "ticks", "ticks"]


def test_run_fixture() -> None:
    mod = _load_script()
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures/lake"
    res = mod.run(runs=1, fixture=fixture_path)

    assert "reads" in res
    reads = res["reads"]
    assert len(reads) == 6

    full_range_rows = [r["rows"] for r in reads if r.get("range_type") == "full"]
    assert full_range_rows == [10, 12, 10]


def test_digest_ticks_changes_on_flag_change() -> None:
    mod = _load_script()
    arrays = {col: np.zeros(5, dtype=_TICK_DTYPE_MAP[col]) for col in COLUMNAR_TICK_KEYS}
    digest1 = mod.digest_ticks(arrays)

    # Change one flags element
    arrays["flags"][2] = 42
    digest2 = mod.digest_ticks(arrays)

    assert digest1 != digest2


def test_compare(tmp_path: Path) -> None:
    mod = _load_script()
    f1 = tmp_path / "reads1.json"
    f2 = tmp_path / "reads2.json"

    dummy_data = {
        "reads": [
            {
                "kind": "bars",
                "symbol": "PETR4",
                "timeframe": "D1",
                "start": "2024-12-16T10:00:00",
                "end": "2025-01-10T10:00:00",
                "range_type": "full",
                "rows": 10,
                "digest": "abc123digest",
                "median_wall_s": 0.001,
                "peak_rss_kib": 1000,
            }
        ]
    }
    f1.write_text(json.dumps(dummy_data), encoding="utf-8")

    # File compared with itself returns 0
    assert mod.compare(f1, f1) == 0

    # One digest differs returns 1
    dummy_diff = {
        "reads": [
            {
                **dummy_data["reads"][0],
                "digest": "different_digest",
            }
        ]
    }
    f2.write_text(json.dumps(dummy_diff), encoding="utf-8")
    assert mod.compare(f1, f2) == 1
