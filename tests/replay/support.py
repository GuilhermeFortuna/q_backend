"""Helpers shared by the replay tests: frozen datasets and virtual-time simulators."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.service import ReplaySimulator
from q_backend.replay.journal import ReplayRunStore

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)  # 09:00 Brasília
BASE_US = int(BASE.timestamp() * 1_000_000)
TRADE = 8 | 16 | 32
QUOTE = 2 | 4


def spec(**overrides) -> dict:
    base = {
        "description": "test future",
        "point": 1.0,
        "digits": 0,
        "volume_min": 1.0,
        "volume_step": 1.0,
        "volume_max": 100.0,
        "trade_contract_size": 1.0,
        "margin_per_contract": "100.00",
        "commission_per_contract": "0",
        "slippage_ticks": 0,
    }
    base.update(overrides)
    return base


def ms(seconds: float) -> int:
    """UTC milliseconds at ``BASE + seconds``."""
    return int(BASE.timestamp() * 1000) + int(round(seconds * 1000))


def write_dataset(
    root: Path,
    ticks_by_symbol: dict[str, list[tuple]],
    specs: dict[str, dict],
    coverage: tuple[datetime, datetime] | None = None,
) -> Path:
    """Freeze ticks (time_msc, bid, ask, last, volume, flags) per symbol and write the manifest."""
    coverage = coverage or (BASE - timedelta(hours=1), BASE + timedelta(hours=2))
    files = []
    for symbol, ticks in ticks_by_symbol.items():
        rows = sorted(ticks, key=lambda row: row[0])
        arrays = {
            "time_msc": np.array([r[0] for r in rows], dtype=np.int64),
            "bid": np.array([r[1] for r in rows], dtype=np.float64),
            "ask": np.array([r[2] for r in rows], dtype=np.float64),
            "last": np.array([r[3] for r in rows], dtype=np.float64),
            "volume": np.array([r[4] for r in rows], dtype=np.float64),
            "flags": np.array([r[5] for r in rows], dtype=np.int32),
        }
        name = f"{symbol.replace('$', '')}.npz"
        np.savez(root / name, **arrays)
        content = (root / name).read_bytes()
        files.append(
            {"path": name, "symbol": symbol, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
        )
    manifest = {
        "schema_version": 1,
        "time_unit": "msc",
        "exchange_timezone": "America/Sao_Paulo",
        "coverage_start_utc": coverage[0].isoformat(),
        "coverage_end_utc": coverage[1].isoformat(),
        "files": files,
        "symbol_specs": specs,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def config_for(
    *,
    symbols=("WIN$N",),
    end_seconds: float = 60,
    latency: dict | None = None,
    balance: str = "100000.00",
    rate: float = 1.0,
    start: datetime = BASE,
) -> ReplayRunConfig:
    return ReplayRunConfig.model_validate(
        {
            "manifest_path": "manifest.json",
            "symbols": list(symbols),
            "start_utc": start,
            "end_utc": start + timedelta(seconds=end_seconds),
            "initial_balance": balance,
            "account": {"login": 900001, "currency": "BRL"},
            "latency": latency or {},
            "rate": rate,
            "limits": {"max_queued_events": 10_000, "max_journal_bytes": 50_000_000},
        }
    )


class VirtualTime:
    """Monotonic clock the test advances explicitly, in nanoseconds."""

    def __init__(self) -> None:
        self.ns = 10**12

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, value: float) -> None:
        self.ns += int(round(value * 1_000_000))


def virtual_simulator(
    tmp_path: Path, clock: VirtualTime, ticks_by_symbol, specs, config: ReplayRunConfig, *, run_id: str = "run-1"
) -> ReplaySimulator:
    write_dataset(tmp_path, ticks_by_symbol, specs)
    store = ReplayRunStore(tmp_path / "replay.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path, mono_ns=clock)
    simulator.create(config, run_id=run_id, generation="gen-1")
    return simulator


def reopen(tmp_path: Path, clock: VirtualTime) -> ReplaySimulator:
    store = ReplayRunStore(tmp_path / "replay.sqlite3")
    return ReplaySimulator(store, dataset_root=tmp_path, mono_ns=clock)
