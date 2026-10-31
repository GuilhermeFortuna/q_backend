"""Freezing a research TickStore range: read-only, UTC conversion, checksums, refusal of gaps."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest

from q_backend.replay.dataset import load_manifest, verify_manifest
from q_backend.replay.import_tickstore import TickStoreImportError, freeze_tickstore
from q_backend.research.tick_store import _write_day_atomic

SPEC = {
    "point": 1.0,
    "digits": 0,
    "volume_min": 1.0,
    "volume_step": 1.0,
    "volume_max": 100.0,
    "trade_contract_size": 1.0,
    "margin_per_contract": "100.00",
}
DAY = date(2026, 10, 1)


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "tick_store"
    # Broker wall clock 09:00:00 Brasília, encoded as UTC epoch (the MT5 wire convention).
    wire_base = int(datetime(2026, 10, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
    arrays = {
        "time_msc": np.array([wire_base + 100, wire_base, wire_base + 100], dtype=np.int64),
        "bid": np.array([99.0, 99.0, 98.0]),
        "ask": np.array([100.0, 100.0, 99.0]),
        "last": np.array([100.0, 99.5, 98.5]),
        "volume": np.array([1.0, 2.0, 3.0]),
        "flags": np.array([56, 56, 56], dtype=np.int32),
    }
    _write_day_atomic(root / "WIN$N" / f"{DAY.isoformat()}.parquet", arrays)
    return root


def test_freeze_converts_wire_time_to_utc_and_verifies(tmp_path) -> None:
    root = _store(tmp_path)
    before = (root / "WIN$N" / f"{DAY.isoformat()}.parquet").read_bytes()
    out = tmp_path / "frozen"
    manifest_path = freeze_tickstore(
        symbol="WIN$N",
        store_root=root,
        start_day=DAY,
        end_day=DAY,
        out_dir=out,
        specs=SPEC,
        coverage_start_utc=datetime(2026, 10, 1, 11, tzinfo=UTC),
        coverage_end_utc=datetime(2026, 10, 1, 18, tzinfo=UTC),
    )
    # The store is read, never written.
    assert (root / "WIN$N" / f"{DAY.isoformat()}.parquet").read_bytes() == before
    manifest = load_manifest(manifest_path)
    verify_manifest(
        manifest,
        out,
        symbols=["WIN$N"],
        start_utc=datetime(2026, 10, 1, 12, tzinfo=UTC),
        end_utc=datetime(2026, 10, 1, 13, tzinfo=UTC),
    )
    frozen = np.load(out / "WINN.npz")
    # 09:00 Brasília is 12:00 UTC; the ticks are sorted in UTC with duplicates kept.
    expected_first = int(datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert frozen["time_msc"][0] == expected_first
    assert list(frozen["time_msc"] - expected_first) == [0, 100, 100]
    assert list(frozen["last"]) == [99.5, 100.0, 98.5]


def test_missing_session_is_refused(tmp_path) -> None:
    root = _store(tmp_path)
    with pytest.raises(TickStoreImportError, match="no session"):
        freeze_tickstore(
            symbol="WIN$N",
            store_root=root,
            start_day=DAY,
            end_day=date(2026, 10, 2),
            out_dir=tmp_path / "out",
            specs=SPEC,
            coverage_start_utc=datetime(2026, 10, 1, tzinfo=UTC),
            coverage_end_utc=datetime(2026, 10, 3, tzinfo=UTC),
        )


def test_cli_freezes_selected_tickstore_sessions(tmp_path, capsys):
    from q_backend.cli.q_replay import main

    root = _store(tmp_path)
    specs = tmp_path / "spec.json"
    specs.write_text(json.dumps(SPEC))
    out = tmp_path / "frozen"
    result = main(
        [
            "freeze-tickstore",
            "--symbol",
            "WIN$N",
            "--store-root",
            str(root),
            "--start-day",
            "2026-10-01",
            "--end-day",
            "2026-10-01",
            "--out-dir",
            str(out),
            "--spec",
            str(specs),
            "--coverage-start-utc",
            "2026-10-01T11:00:00Z",
            "--coverage-end-utc",
            "2026-10-01T18:00:00Z",
        ]
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["manifest_path"] == str(out / "manifest.json")
    manifest = load_manifest(out / "manifest.json")
    assert manifest.source["tickstore_symbol"] == "WIN$N"
    assert manifest.files[0].sha256 == hashlib.sha256((out / "WINN.npz").read_bytes()).hexdigest()
