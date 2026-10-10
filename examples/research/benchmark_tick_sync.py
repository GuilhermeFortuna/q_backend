"""Opt-in MT5 benchmark; writes only temporary research stores.

Run against a gateway updated to support /v1/ticks/page. Native/encoding timings
are emitted by that gateway; this script measures HTTP, decode, disk and memory.
It never clears the terminal cache or alters existing tick-store sessions.
"""

from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.research import TickStore
from q_backend.research.tick_store import _day_bounds, _iter_weekdays, _write_day_atomic


class MeasuredClient(RemoteMt5Client):
    """Collect HTTP and decoding costs at the existing client boundaries."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.http_seconds = 0.0
        self.decode_seconds = 0.0
        self.requests = 0
        self.bytes_received = 0

    def _get_npz(self, path, params):
        started = time.perf_counter()
        try:
            content = super()._get_npz(path, params)
            self.bytes_received += len(content)
            return content
        finally:
            self.requests += 1
            self.http_seconds += time.perf_counter() - started

    def _decode_timed(self, call, *args, **kwargs):
        started, before = time.perf_counter(), self.http_seconds
        try:
            return call(*args, **kwargs)
        finally:
            self.decode_seconds += time.perf_counter() - started - (self.http_seconds - before)

    def get_tick_page(self, *args, **kwargs):
        return self._decode_timed(super().get_tick_page, *args, **kwargs)

    def get_ticks_columnar(self, *args, **kwargs):
        return self._decode_timed(super().get_ticks_columnar, *args, **kwargs)


def run_case(symbol, days, url, token, root, *, legacy):
    client = MeasuredClient(base_url=url, token=token)
    store = TickStore(symbol, root=root)
    disk_seconds = 0.0
    original_write = pq.ParquetWriter.write_table

    def timed_write(writer, table, *args, **kwargs):
        nonlocal disk_seconds
        started = time.perf_counter()
        try:
            return original_write(writer, table, *args, **kwargs)
        finally:
            disk_seconds += time.perf_counter() - started

    started = time.perf_counter()
    if legacy:
        for day in days:
            start, end = _day_bounds(day)
            first = client.get_ticks_columnar(symbol, start, end, flags=COPY_TICKS_ALL, use_cache=False)
            second = client.get_ticks_columnar(symbol, start, end, flags=COPY_TICKS_ALL, use_cache=False)
            if len(first["time_msc"]) != len(second["time_msc"]):
                raise RuntimeError(f"{day}: legacy verification returned different row counts")
            if len(second["time_msc"]):
                write_started = time.perf_counter()
                _write_day_atomic(store._day_path(day), second)
                disk_seconds += time.perf_counter() - write_started
    else:
        with (
            patch("q_backend.research.tick_store.RemoteMt5Client", return_value=client),
            patch.object(
                pq.ParquetWriter,
                "write_table",
                timed_write,
            ),
        ):
            report = store.sync(start=days[0].isoformat(), end=days[-1].isoformat(), gateway_url=url, progress=False)
        if report.failed:
            raise RuntimeError(str(report))
    elapsed = time.perf_counter() - started
    return store, {
        "total_seconds": elapsed,
        "http_seconds": client.http_seconds,
        "decode_seconds": client.decode_seconds,
        "disk_seconds": disk_seconds,
        "requests": client.requests,
        "bytes_received": client.bytes_received,
        "rows": sum(pq.read_metadata(store._day_path(day)).num_rows for day in store.sessions()),
        # Lifetime high-water mark: cases share a process, so this is not an
        # independent per-case peak or evidence of a memory improvement.
        "cumulative_process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def compare(symbol, days, url, token, *, optimized_first=False):
    results, stores = {}, {}
    order = ["stream", "legacy"] if optimized_first else ["legacy", "stream"]
    with tempfile.TemporaryDirectory(prefix="q-tick-sync-benchmark-") as root:
        for method in order:
            print(f"{method}: {symbol} {days[0]} through {days[-1]}", flush=True)
            stores[method], results[method] = run_case(
                symbol, days, url, token, Path(root) / method, legacy=method == "legacy"
            )
            print(json.dumps({method: results[method]}), flush=True)
        if stores["legacy"].sessions() != stores["stream"].sessions():
            raise RuntimeError("Baseline and stream returned different sessions")
        for day in stores["legacy"].sessions():
            expected = pq.read_table(stores["legacy"]._day_path(day))
            actual = pq.read_table(stores["stream"]._day_path(day))
            if not expected.equals(actual):
                raise RuntimeError(f"{day}: tick arrays differ between baseline and stream")
        results["speedup"] = results["legacy"]["total_seconds"] / results["stream"]["total_seconds"]
        results["identical_ticks"] = True
        if len(stores["stream"].sessions()) == len(days):
            started = time.perf_counter()
            with patch("q_backend.research.tick_store.RemoteMt5Client", side_effect=AssertionError("unexpected HTTP")):
                stores["stream"].sync(start=days[0].isoformat(), end=days[-1].isoformat(), progress=False)
            results["repeat_sync_seconds"] = time.perf_counter() - started
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--empty-day", help="Optional known-empty date; benchmarked separately")
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--output", type=Path, help="Optional JSON measurement artifact")
    parser.add_argument("--optimized-first", action="store_true", help="Reverse order to expose warm-cache bias")
    args = parser.parse_args(argv)
    from q_backend.research.providers import resolve_gateway_token

    token = resolve_gateway_token(None)
    days = _iter_weekdays(date.fromisoformat(args.start), date.fromisoformat(args.end))
    if not days or days[-1] >= date.today():
        parser.error("Choose closed weekday sessions ending before today")
    cases = {"populated_day": [days[-1]]}
    if len(days) > 1:
        cases["populated_window"] = days
    if args.empty_day:
        empty = date.fromisoformat(args.empty_day)
        if empty >= date.today() or empty.weekday() >= 5:
            parser.error("--empty-day must be a closed weekday")
        cases["empty_day"] = [empty]
    output = {
        "symbol": args.symbol,
        "cache_policy": "terminal cache retained; no controlled cold-cache claim",
        "native_timings": "see gateway Tick page/Fetching MT5 ticks and encoding logs",
        "cases": {
            name: compare(args.symbol, dates, args.gateway_url, token, optimized_first=args.optimized_first)
            for name, dates in cases.items()
        },
    }
    text = json.dumps(output, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
