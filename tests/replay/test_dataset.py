"""Immutable dataset manifest verification for replay runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from q_backend.replay.dataset import DatasetError, load_manifest, verify_manifest

SPEC = {
    "point": 1.0,
    "digits": 0,
    "volume_min": 1.0,
    "volume_step": 1.0,
    "volume_max": 100.0,
    "trade_contract_size": 1.0,
    "margin_per_contract": "1000.00",
}
COVER_START = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
COVER_END = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


def _write_tick_file(root: Path, name: str, content: bytes) -> dict:
    (root / "ticks").mkdir(parents=True, exist_ok=True)
    (root / "ticks" / name).write_bytes(content)
    return {
        "path": f"ticks/{name}",
        "symbol": name.split(".")[0],
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Path, Path]:
    files = [
        _write_tick_file(tmp_path, "WDO.ticks", b"wdo-tick-bytes"),
        _write_tick_file(tmp_path, "WIN.ticks", b"win-tick-bytes"),
    ]
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": "America/Sao_Paulo",
        "coverage_start_utc": COVER_START.isoformat(),
        "coverage_end_utc": COVER_END.isoformat(),
        "files": files,
        "symbol_specs": {"WDO": SPEC, "WIN": SPEC},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, tmp_path


def _verify(path: Path, root: Path, **overrides):
    manifest = load_manifest(path)
    kwargs = {
        "symbols": ["WDO", "WIN"],
        "start_utc": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return verify_manifest(manifest, root, **kwargs)


def test_valid_dataset_returns_checksums_per_file(dataset) -> None:
    path, root = dataset
    checksums = _verify(path, root)
    assert len(checksums) == 2
    assert all(item.startswith("sha256:") and len(item) == len("sha256:") + 64 for item in checksums)


def test_mutated_source_file_is_refused(dataset) -> None:
    path, root = dataset
    (root / "ticks" / "WDO.ticks").write_bytes(b"edited-after-prepare!")
    with pytest.raises(DatasetError, match="changed since the manifest"):
        _verify(path, root)


def test_missing_source_file_is_refused(dataset) -> None:
    path, root = dataset
    (root / "ticks" / "WIN.ticks").unlink()
    with pytest.raises(DatasetError, match="missing"):
        _verify(path, root)


def test_path_escaping_the_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.ticks"
    outside.write_bytes(b"x")
    root = tmp_path / "dataset"
    root.mkdir()
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": "America/Sao_Paulo",
        "coverage_start_utc": COVER_START.isoformat(),
        "coverage_end_utc": COVER_END.isoformat(),
        "symbol_specs": {"WDO": SPEC},
        "files": [
            {
                "path": "../outside.ticks",
                "symbol": "WDO",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
            }
        ],
        "symbol_specs": {"WDO": SPEC, "WIN": SPEC},
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetError, match="escapes its root"):
        _verify(path, root, symbols=["WDO"])


def test_requested_symbol_must_be_in_the_dataset(dataset) -> None:
    path, root = dataset
    with pytest.raises(DatasetError, match="not in the dataset: DOL"):
        _verify(path, root, symbols=["WDO", "DOL"])


def test_interval_outside_coverage_is_refused(dataset) -> None:
    path, root = dataset
    with pytest.raises(DatasetError, match="outside the dataset coverage"):
        _verify(path, root, end_utc=COVER_END + timedelta(minutes=1))


def test_warmup_before_coverage_is_refused(dataset) -> None:
    path, root = dataset
    early = COVER_START - timedelta(minutes=1)
    with pytest.raises(DatasetError, match="warmup and interval"):
        _verify(path, root, warmup_start_utc=early)


def test_warmup_inside_coverage_is_accepted(dataset) -> None:
    path, root = dataset
    checksums = _verify(path, root, warmup_start_utc=COVER_START + timedelta(minutes=5))
    assert len(checksums) == 2


def test_manifest_rejects_duplicate_paths(tmp_path: Path) -> None:
    entry = {"path": "ticks/A.ticks", "symbol": "A", "sha256": "0" * 64, "size_bytes": 0}
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": "America/Sao_Paulo",
        "coverage_start_utc": COVER_START.isoformat(),
        "coverage_end_utc": COVER_END.isoformat(),
        "files": [entry, dict(entry)],
        "symbol_specs": {"WDO": SPEC, "WIN": SPEC},
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetError, match="more than once"):
        load_manifest(path)
