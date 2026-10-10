"""Tick import display lifecycle and terminal defaults."""

import io
from datetime import datetime

import pytest
from rich.console import Console

from q_backend.research.progress import tick_import_progress, tick_sync_progress


def test_tick_import_display_reports_rows_and_elapsed_time():
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=160)
    with tick_import_progress(True, "WDO$", console=console) as reporter:
        reporter.start(2)
        reporter.waiting(datetime(2026, 6, 2), datetime(2026, 6, 2, 23, 59))
        reporter.advance(1, 1234)
        reporter.advance(2, 2345)
        reporter.processing()
        reporter.finish(2345)
    assert "WDO$ ticks: 2,345 ticks imported in" in output.getvalue()
    assert not reporter._progress.live.is_started


def test_tick_import_is_quiet_when_redirected_and_stops_on_error():
    output = io.StringIO()
    console = Console(file=output, force_terminal=False)
    with pytest.raises(ConnectionError), tick_import_progress(None, "WDO$", console=console) as reporter:
        reporter.start(2)
        raise ConnectionError("gateway unavailable")
    assert output.getvalue() == ""
    assert not reporter._progress.live.is_started
    assert reporter._progress.tasks[0].fields["status"] == "failed; import incomplete"


def test_eta_retains_samples_from_slow_history_chunks():
    reporter = tick_import_progress(False, "WDO$")
    clock = [0.0]
    reporter._progress.get_time = lambda: clock[0]
    with reporter:
        reporter.start(10)
        clock[0] = 80.0
        reporter.advance(1, 100)
        clock[0] = 160.0
        reporter.advance(2, 200)
        assert reporter._progress.tasks[0].time_remaining == 640


def test_sync_eta_retains_samples_from_slow_sessions():
    from datetime import date

    reporter = tick_sync_progress(False, "WDO$")
    clock = [0.0]
    reporter._progress.get_time = lambda: clock[0]
    with reporter:
        reporter.start(10)
        clock[0] = 80.0
        reporter.advance(1, date(2026, 10, 1), "stored")
        clock[0] = 160.0
        reporter.advance(2, date(2026, 10, 2), "stored")
        assert reporter._progress.tasks[0].time_remaining == 640


def test_sync_completion_retains_interval_throughput_and_active_download_state():
    from datetime import date

    reporter = tick_sync_progress(False, "WDO$")
    with reporter:
        reporter.start(2)
        reporter.waiting(date(2026, 10, 1), date(2026, 10, 2))
        reporter.received(250_000)
        reporter.advance(1, date(2026, 10, 1), "stored")
        fields = reporter._progress.tasks[0].fields
        assert fields["interval"] == "2026-10-01–2026-10-02"
        assert "250,000 ticks" in fields["throughput"]
        assert fields["session"] == "2026-10-01 stored"
        assert "downloading" in fields["status"]
