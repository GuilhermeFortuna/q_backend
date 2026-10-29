from datetime import date, datetime, timezone
from threading import Event, Thread
from unittest.mock import patch

import numpy as np
import pyarrow.parquet as pq
import pytest

from q_backend.market_data.clients.shared import TickPage, TickPagingUnsupportedError
from q_backend.research import TickStore
from q_backend.research.tick_store import _write_day_atomic
from tests.research.test_tick_store import _columnar_for_day

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get_tick_page(self, symbol, start, end, **kwargs):
        self.calls.append((start, end))
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


def page(days, cursor=None):
    chunks = [_columnar_for_day(day, rows) for day, rows in days]
    columns = {key: np.concatenate([chunk[key] for chunk in chunks]) for key in chunks[0]}
    return TickPage(columns, cursor, cursor is None)


def sync(store, client, **kwargs):
    with (
        patch("q_backend.research.tick_store.RemoteMt5Client", return_value=client),
        patch(
            "q_backend.research.tick_store._exchange_now",
            return_value=datetime(2026, 10, 8, 15, tzinfo=timezone.utc),
        ),
    ):
        return store.sync(start="2026-10-05", end="2026-10-07", gateway_url="http://gw.test", **kwargs)


def test_sync_stream_skips_empty_day_and_preserves_duplicates(tmp_path):
    client = Pages([page([(MON, [(0, 100, 32), (0, 100, 32)]), (WED, [(0, 102, 32)])])])
    store = TickStore("WIN$", root=tmp_path)
    report = sync(store, client)
    assert report.stored == [MON, WED]
    assert report.empty == [TUE]
    assert report.unsettled == []
    assert len(client.calls) == 1
    assert pq.read_table(store._day_path(MON))["last"].to_pylist() == [100, 100]


def test_sync_commits_only_days_crossed_by_successful_cursor(tmp_path):
    first = page(
        [(MON, [(0, 100, 32)]), (TUE, [(0, 101, 32)])], int(_columnar_for_day(TUE, [(1, 100, 32)])["time_msc"][0])
    )
    client = Pages([first, ConnectionError("broker disconnected")])
    store = TickStore("WIN$", root=tmp_path)
    report = sync(store, client)
    assert report.stored == [MON]
    assert report.failed == [TUE, WED]
    assert not store.has_session(TUE)
    assert not list(tmp_path.rglob("*.tmp"))
    assert "broker disconnected" in report.errors[TUE]


def test_repeat_sync_returns_locally_without_gateway_configuration(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    for day in (MON, TUE, WED):
        _write_day_atomic(store._day_path(day), _columnar_for_day(day, [(0, 100, 32)]))
    with patch("q_backend.research.tick_store.resolve_gateway_url", return_value=None):
        report = store.sync(start="2026-10-05", end="2026-10-07")
    assert report.already_present == [MON, TUE, WED]


def test_old_gateway_error_is_actionable_and_not_swallowed(tmp_path):
    with pytest.raises(TickPagingUnsupportedError, match="restart"):
        sync(TickStore("WIN$", root=tmp_path), Pages([TickPagingUnsupportedError("restart gateway")]))


def test_fetch_runs_ahead_while_writer_is_busy(tmp_path):
    fetched = Event()
    first = page([(MON, [(0, 100, 32)])], int(_columnar_for_day(TUE, [(0, 100, 32)])["time_msc"][0]))
    second = page([(TUE, [(0, 101, 32)]), (WED, [(0, 102, 32)])])
    client = Pages([first, second])
    original_fetch = client.get_tick_page

    def fetch(*args, **kwargs):
        result = original_fetch(*args, **kwargs)
        if result is second:
            fetched.set()
        return result

    client.get_tick_page = fetch
    original_write = pq.ParquetWriter.write_table

    def write(writer, table, *args, **kwargs):
        assert fetched.wait(2), "fetch must overlap the write"
        return original_write(writer, table, *args, **kwargs)

    with patch.object(pq.ParquetWriter, "write_table", write):
        report = sync(TickStore("WIN$", root=tmp_path), client)
    assert report.stored == [MON, TUE, WED]


def test_writer_failure_removes_partial_file_and_can_resume(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    with patch.object(pq.ParquetWriter, "write_table", side_effect=OSError("disk full")):
        report = sync(store, Pages([page([(MON, [(0, 100, 32)])])]))
    assert report.failed == [MON, TUE, WED]
    assert not store.has_session(MON)
    assert not list(tmp_path.rglob("*.tmp"))
    report = sync(store, Pages([page([(MON, [(0, 100, 32)]), (TUE, [(0, 101, 32)]), (WED, [(0, 102, 32)])])]))
    assert report.stored == [MON, TUE, WED]


def test_streamed_bars_equal_bars_from_original_ordered_ticks(tmp_path):
    expected = TickStore("WIN$", root=tmp_path / "expected")
    actual = TickStore("WIN$", root=tmp_path / "actual")
    rows = [(0, 100, 32), (0, 101, 32), (1, 99, 32), (2, 102, 32)]
    _write_day_atomic(expected._day_path(MON), _columnar_for_day(MON, rows))
    split = int(_columnar_for_day(MON, rows[2:])["time_msc"][0])
    sync(actual, Pages([page([(MON, rows[:2])], split), page([(MON, rows[2:])])]))
    import pandas as pd

    pd.testing.assert_frame_equal(
        actual.bars("M1", start="2026-10-05", end="2026-10-06"),
        expected.bars("M1", start="2026-10-05", end="2026-10-06"),
    )


def test_prefetch_is_bounded_and_close_prevents_further_requests():
    from q_backend.research.tick_sync import prefetched_pages

    filled = Event()
    calls = []

    class Source:
        def get_tick_page(self, symbol, start, end):
            calls.append(start)
            if len(calls) == 4:
                filled.set()
            return TickPage(_columnar_for_day(MON, [(0, 100, 32)]), start + 1, False)

    pages = prefetched_pages(Source(), "WIN$", 1000, 2000)
    next(pages)
    assert filled.wait(2)
    assert len(calls) == 4  # consumer page, two queued pages, one producer page
    pages.close()
    assert len(calls) == 4


def test_interrupt_cleans_partial_session(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    with patch.object(pq.ParquetWriter, "write_table", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        sync(store, Pages([page([(MON, [(0, 100, 32)])])]))
    assert not store.has_session(MON)
    assert not list(tmp_path.rglob("*.tmp"))


def test_writer_failure_waits_for_outstanding_fetch_before_next_interval(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    _write_day_atomic(store._day_path(TUE), _columnar_for_day(TUE, [(0, 101, 32)]))
    blocked, release, next_interval, failed = Event(), Event(), Event(), Event()
    first = page([(MON, [(0, 100, 32)])], int(_columnar_for_day(MON, [(1, 100, 32)])["time_msc"][0]))

    class Source:
        calls = 0

        def get_tick_page(self, symbol, start, end):
            self.calls += 1
            if self.calls == 1:
                return first
            if self.calls == 2:
                blocked.set()
                assert release.wait(5)
                return page([(MON, [(1, 100, 32)])])
            next_interval.set()
            return page([(WED, [(0, 102, 32)])])

    def fail_write(*args, **kwargs):
        assert blocked.wait(2)
        failed.set()
        raise OSError("disk full")

    results = []
    with patch.object(pq.ParquetWriter, "write_table", fail_write):
        runner = Thread(target=lambda: results.append(sync(store, Source())))
        runner.start()
        try:
            assert failed.wait(2)
            assert not next_interval.wait(1.2), "must not overlap producers after a write failure"
        finally:
            release.set()
            runner.join(5)
    assert not runner.is_alive()
    assert results[0].failed == [MON, WED]
    assert not list(tmp_path.rglob("*.tmp"))


def test_overwrite_keeps_days_stored_before_a_failure_and_drops_the_rest(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    for day in (MON, TUE):
        _write_day_atomic(store._day_path(day), _columnar_for_day(day, [(0, 1.0, 32)]))
    # The first page completes Monday's session; the stream then fails before Tuesday's end.
    first = page([(MON, [(0, 100, 32)])], int(_columnar_for_day(TUE, [(0, 100, 32)])["time_msc"][0]))
    report = sync(store, Pages([first, ConnectionError("broker disconnected")]), overwrite=True)
    assert report.stored == [MON]
    assert report.failed == [TUE, WED]
    assert pq.read_table(store._day_path(MON))["last"].to_pylist() == [100]
    assert not store.has_session(TUE)


def test_overwrite_removes_stored_days_when_paging_is_unsupported(tmp_path):
    store = TickStore("WIN$", root=tmp_path)
    _write_day_atomic(store._day_path(MON), _columnar_for_day(MON, [(0, 1.0, 32)]))
    with pytest.raises(TickPagingUnsupportedError):
        sync(store, Pages([TickPagingUnsupportedError("restart gateway")]), overwrite=True)
    assert not store.has_session(MON)
