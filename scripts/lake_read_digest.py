#!/usr/bin/env python3
"""Lake read digest and performance benchmark (Q-020).

Computes row counts, content digests (SHA-256), median wall time, and peak RSS
across full range and last calendar month reads for all catalogued datasets.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import tempfile
import time
from typing import Any, Literal
import warnings

import numpy as np

warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*fork.*")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data import local_store
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base


@dataclass(frozen=True)
class ReadSpec:
    kind: Literal["bars", "ticks"]
    symbol: str
    timeframe: str
    start: datetime
    end: datetime


def planned_reads(inventory: list[dict[str, Any]]) -> list[ReadSpec]:
    """Return full range + last calendar month ReadSpecs per dataset in inventory."""
    specs: list[ReadSpec] = []
    for item in inventory:
        kind = item["kind"]
        symbol = item["symbol"]
        tf = item.get("timeframe", "")
        start_dt = datetime.fromisoformat(item["start"])
        end_dt = datetime.fromisoformat(item["end"])

        # 1. Full range read
        specs.append(ReadSpec(kind=kind, symbol=symbol, timeframe=tf, start=start_dt, end=end_dt))

        # 2. Last calendar month read
        month_start = datetime(end_dt.year, end_dt.month, 1)
        if end_dt.tzinfo is not None:
            month_start = month_start.replace(tzinfo=end_dt.tzinfo)
        specs.append(ReadSpec(kind=kind, symbol=symbol, timeframe=tf, start=month_start, end=end_dt))
    return specs


def digest_bars(bars: list[OHLCV]) -> str:
    """Compute sha256 digest over model_dump_json lines."""
    hasher = hashlib.sha256()
    for bar in bars:
        hasher.update((bar.model_dump_json() + "\n").encode("utf-8"))
    return hasher.hexdigest()


def digest_ticks(arrays: dict[str, np.ndarray]) -> str:
    """Compute sha256 digest over key, dtype.str, bytes, in key order."""
    hasher = hashlib.sha256()
    for key in sorted(arrays.keys()):
        arr = arrays[key]
        hasher.update(key.encode("utf-8"))
        hasher.update(arr.dtype.str.encode("utf-8"))
        hasher.update(np.ascontiguousarray(arr).tobytes())
    return hasher.hexdigest()


def _measure_peak_rss(spec: ReadSpec) -> int:
    """Measure peak RSS KiB of a read in a forked child process."""
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        try:
            if spec.kind == "bars":
                local_store.read_ohlcv(spec.symbol, spec.timeframe, spec.start, spec.end)
            else:
                local_store.read_ticks_columnar(spec.symbol, spec.start, spec.end)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            os.write(w_fd, str(rss).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            os.write(w_fd, f"ERROR:{exc}".encode("utf-8"))
        finally:
            os.close(w_fd)
            os._exit(0)
    else:
        os.close(w_fd)
        with os.fdopen(r_fd, "r") as f:
            out = f.read()
        os.waitpid(pid, 0)
        if out.startswith("ERROR:"):
            raise RuntimeError(out)
        return int(out) if out else 0


def _execute_reads(specs: list[ReadSpec], runs: int) -> list[dict[str, Any]]:
    read_dicts: list[dict[str, Any]] = []
    for i, spec in enumerate(specs):
        range_type = "full" if i % 2 == 0 else "last_month"
        wall_times: list[float] = []
        rows = 0
        digest = ""

        for r in range(runs):
            t0 = time.perf_counter()
            if spec.kind == "bars":
                bars = local_store.read_ohlcv(spec.symbol, spec.timeframe, spec.start, spec.end)
                t1 = time.perf_counter()
                if r == 0:
                    rows = len(bars)
                    digest = digest_bars(bars)
            else:
                arrays = local_store.read_ticks_columnar(spec.symbol, spec.start, spec.end)
                t1 = time.perf_counter()
                if r == 0:
                    rows = len(arrays["time_msc"]) if arrays and "time_msc" in arrays else 0
                    digest = digest_ticks(arrays)
            wall_times.append(t1 - t0)

        median_wall_s = statistics.median(wall_times)
        peak_rss_kib = _measure_peak_rss(spec)

        read_dicts.append(
            {
                "kind": spec.kind,
                "symbol": spec.symbol,
                "timeframe": spec.timeframe,
                "start": spec.start.isoformat(),
                "end": spec.end.isoformat(),
                "range_type": range_type,
                "rows": rows,
                "digest": digest,
                "median_wall_s": median_wall_s,
                "peak_rss_kib": peak_rss_kib,
            }
        )
    return read_dicts


def run(runs: int = 1, fixture: Path | None = None) -> dict[str, Any]:
    """Execute planned reads and return row counts, digests, median wall s, and peak RSS."""
    if fixture is not None:
        fixture_path = Path(fixture).resolve()
        with tempfile.TemporaryDirectory() as tmp_dir:
            dst_lake = Path(tmp_dir) / "lake"
            shutil.copytree(fixture_path, dst_lake)
            orig_root = os.environ.get("Q_MARKET_DATA_ROOT")
            orig_instance = getattr(catalog_service, "_catalog_instance", None)
            os.environ["Q_MARKET_DATA_ROOT"] = str(dst_lake)
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            sm = sessionmaker(bind=engine)
            catalog = LakeCatalog(session_factory=sm, root=dst_lake)
            catalog.adopt()
            catalog_service._catalog_instance = catalog
            try:
                inventory = local_store.list_inventory()
                specs = planned_reads(inventory)
                reads = _execute_reads(specs, runs)
                return {
                    "datasets_count": len(inventory),
                    "reads_count": len(specs),
                    "reads": reads,
                }
            finally:
                if orig_root is not None:
                    os.environ["Q_MARKET_DATA_ROOT"] = orig_root
                else:
                    os.environ.pop("Q_MARKET_DATA_ROOT", None)
                catalog_service._catalog_instance = orig_instance

    inventory = local_store.list_inventory()
    specs = planned_reads(inventory)
    reads = _execute_reads(specs, runs)
    return {
        "datasets_count": len(inventory),
        "reads_count": len(specs),
        "reads": reads,
    }


def compare(before: Path, after: Path) -> int:
    """Compare two read digest JSON files; return 0 if all rows and digests match, 1 otherwise."""
    before_data = json.loads(before.read_text(encoding="utf-8"))
    after_data = json.loads(after.read_text(encoding="utf-8"))
    before_reads = before_data.get("reads", [])
    after_reads = after_data.get("reads", [])

    if len(before_reads) != len(after_reads):
        print(f"Read count mismatch: {len(before_reads)} vs {len(after_reads)}")
        return 1

    all_match = True
    for i, (b, a) in enumerate(zip(before_reads, after_reads)):
        ident = f"{b['symbol']} {b['kind']} {b.get('timeframe', '')} ({b.get('range_type', '')})"
        if b.get("rows") != a.get("rows"):
            print(f"Row count mismatch for read #{i} {ident}: before={b.get('rows')}, after={a.get('rows')}")
            all_match = False
        if b.get("digest") != a.get("digest"):
            print(f"Digest mismatch for read #{i} {ident}: before={b.get('digest')}, after={a.get('digest')}")
            all_match = False

    if not all_match:
        return 1

    print(f"All {len(before_reads)} reads matched across {before_data.get('datasets_count', 0)} datasets.")
    tick_reads_before = [r for r in before_reads if r["kind"] == "ticks"]
    tick_reads_after = [r for r in after_reads if r["kind"] == "ticks"]
    if tick_reads_before and tick_reads_after:
        print("\nTick reads performance comparison:")
        for b, a in zip(tick_reads_before, tick_reads_after):
            ident = f"{b['symbol']} {b.get('range_type', '')} (rows: {b['rows']})"
            print(f"  {ident}:")
            print(f"    before: median_wall_s={b['median_wall_s']:.6f}s, peak_rss={b['peak_rss_kib']} KiB")
            print(f"    after:  median_wall_s={a['median_wall_s']:.6f}s, peak_rss={a['peak_rss_kib']} KiB")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lake read digest & performance benchmark")
    parser.add_argument("--runs", type=int, default=1, help="Number of repetitions per read (default: 1)")
    parser.add_argument("--fixture", type=Path, default=None, help="Path to fixture directory")
    parser.add_argument(
        "--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"), help="Compare two read JSON files"
    )
    args = parser.parse_args(argv)

    if args.compare:
        return compare(args.compare[0], args.compare[1])

    result = run(runs=args.runs, fixture=args.fixture)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
