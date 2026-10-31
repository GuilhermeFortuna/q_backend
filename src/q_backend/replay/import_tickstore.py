"""Freeze an existing research TickStore range into replay inputs (read-only).

TickStore stores one parquet per session day with broker wall-clock times (the MT5 wire convention).
This importer reads the selected days without syncing or writing to the store, converts the times to
UTC milliseconds, writes one NPZ per symbol, and records each file's checksum in a manifest that
``q-replay prepare`` and the simulator verify before a run starts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from q_backend.replay.market import wire_to_utc_ms_array
from q_backend.research.tick_store import TickStore, _COLUMNAR_KEYS, _read_day_columnar

EXCHANGE_TZ = "America/Sao_Paulo"


class TickStoreImportError(ValueError):
    """The requested TickStore range cannot be frozen (missing days or empty range)."""


def freeze_tickstore(
    *,
    symbol: str,
    store_root: Path,
    start_day: date,
    end_day: date,
    out_dir: Path,
    specs: dict,
    coverage_start_utc: datetime,
    coverage_end_utc: datetime,
) -> Path:
    """Write ``<out_dir>/<symbol>.npz`` and ``<out_dir>/manifest.json``; return the manifest path."""
    if end_day < start_day:
        raise TickStoreImportError("end day is before start day")
    zone = ZoneInfo(EXCHANGE_TZ)
    store = TickStore(symbol, root=store_root)
    sessions = [start_day + timedelta(days=offset) for offset in range((end_day - start_day).days + 1)]
    missing = [day for day in sessions if not store.has_session(day)]
    if missing:
        raise TickStoreImportError(f"TickStore has no session for {', '.join(d.isoformat() for d in missing)}")

    parts: dict[str, list[np.ndarray]] = {key: [] for key in _COLUMNAR_KEYS}
    for day in sessions:
        path = store.session_path(day)
        if path is None:
            raise TickStoreImportError(f"TickStore session {day} has no file")
        columns = _read_day_columnar(path)
        for key in _COLUMNAR_KEYS:
            parts[key].append(columns[key])
    merged = {key: np.concatenate(parts[key]) if parts[key] else np.array([]) for key in _COLUMNAR_KEYS}
    if merged["time_msc"].size == 0:
        raise TickStoreImportError("the selected TickStore range holds no ticks")

    utc = wire_to_utc_ms_array(merged["time_msc"].astype(np.int64), zone)
    order = np.argsort(utc, kind="stable")
    arrays = {"time_msc": utc[order]}
    for key in ("bid", "ask", "last", "volume", "flags"):
        arrays[key] = np.asarray(merged[key])[order]

    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / f"{symbol.replace('$', '')}.npz"
    np.savez(npz_path, **arrays)
    content = npz_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": EXCHANGE_TZ,
        "coverage_start_utc": coverage_start_utc.isoformat(),
        "coverage_end_utc": coverage_end_utc.isoformat(),
        "files": [
            {
                "path": npz_path.name,
                "symbol": symbol,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
        "symbol_specs": {symbol: specs},
        "source": {"tickstore_symbol": symbol, "first_day": start_day.isoformat(), "last_day": end_day.isoformat()},
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
