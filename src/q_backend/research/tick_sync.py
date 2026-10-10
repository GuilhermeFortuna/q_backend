"""Bounded tick download prefetch and atomic, incremental daily Parquet writes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import TickPage

_EPOCH = datetime(1970, 1, 1)


def day_msc(day: date) -> int:
    """Raw broker wall-clock epoch, matching the existing tick-store convention."""
    return (datetime.combine(day, datetime.min.time()) - _EPOCH) // timedelta(milliseconds=1)


def prefetched_pages(client: RemoteMt5Client, symbol: str, start: int, end: int) -> Iterator[TickPage]:
    """One producer, at most two queued pages; only the consumer touches disk."""
    pending: Queue[TickPage | Exception | None] = Queue(maxsize=2)
    cancelled = Event()

    def put(item: TickPage | Exception | None) -> bool:
        while not cancelled.is_set():
            try:
                pending.put(item, timeout=0.1)
                return True
            except Full:
                pass
        return False

    def fetch() -> None:
        cursor = start
        try:
            while not cancelled.is_set():
                page = client.get_tick_page(symbol, cursor, end)
                if not put(page) or page.exhausted:
                    break
                if page.next_cursor_msc is None or page.next_cursor_msc <= cursor:
                    raise ConnectionError("Tick page cursor did not advance.")
                cursor = page.next_cursor_msc
        except Exception as exc:  # noqa: BLE001 - transfer failures to the consumer
            put(exc)
        finally:
            put(None)

    producer = Thread(target=fetch, name="tick-sync-download", daemon=True)
    producer.start()
    try:
        while True:
            try:
                item = pending.get(timeout=0.1)
            except Empty:
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        cancelled.set()
        # The transport timeout bounds an outstanding native request. Drain that
        # request before another interval/retry can acquire the same gateway lane.
        # Merely abandoning a daemon here creates overlapping download lifetimes.
        producer.join()


class SessionWriter:
    """Stream ordered tick row groups; expose the final path only after completion."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.temporary: Path | None = None
        self.writer: pq.ParquetWriter | None = None
        self.rows = 0

    def append(self, columns: dict[str, np.ndarray], first: int, stop: int) -> None:
        if first == stop:
            return
        table = pa.table({key: values[first:stop] for key, values in columns.items()})
        if self.writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".parquet.tmp")
            os.close(fd)
            self.temporary = Path(name)
            self.writer = pq.ParquetWriter(name, table.schema, compression="zstd")
        self.writer.write_table(table)
        self.rows += stop - first

    def commit(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
            assert self.temporary is not None
            os.replace(self.temporary, self.path)
            self.temporary = None

    def abort(self) -> None:
        try:
            if self.writer is not None:
                self.writer.close()
        finally:
            self.writer = None
            if self.temporary is not None:
                self.temporary.unlink(missing_ok=True)
                self.temporary = None


def write_interval(
    client: RemoteMt5Client,
    symbol: str,
    days: list[date],
    path_for: Callable[[date], Path],
    completed: Callable[[date, int], None],
    received: Callable[[TickPage], None],
) -> list[date]:
    """Commit days only when a successful cursor has passed their exclusive end.

    The caller owns failure reporting. ``completed`` is called after each atomic
    commit, including successful scans with no rows for a requested weekday.
    """
    end = day_msc(days[-1] + timedelta(days=1))
    index = 0
    writer = SessionWriter(path_for(days[0]))
    pages = prefetched_pages(client, symbol, day_msc(days[0]), end)
    try:
        for page in pages:
            received(page)
            times = page.columns["time_msc"]
            while index < len(days):
                day = days[index]
                next_midnight = day_msc(day + timedelta(days=1))
                first, stop = np.searchsorted(times, [day_msc(day), next_midnight])
                writer.append(page.columns, int(first), int(stop))
                coverage_stop = end if page.exhausted else page.next_cursor_msc
                if coverage_stop is None or coverage_stop < next_midnight:
                    break
                writer.commit()
                completed(day, writer.rows)
                index += 1
                if index < len(days):
                    writer = SessionWriter(path_for(days[index]))
    finally:
        try:
            writer.abort()
        finally:
            pages.close()
    return days[index:]
