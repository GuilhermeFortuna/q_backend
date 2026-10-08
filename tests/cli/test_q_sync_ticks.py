"""`q-sync-ticks` forwards its arguments to the research tick store."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from q_backend.cli import q_sync_ticks
from q_backend.research.tick_store import TickStore, TickSyncReport


def test_main_syncs_the_requested_range_and_prints_the_report(tmp_path, capsys) -> None:
    report = TickSyncReport(stored=[date(2026, 10, 5)], already_present=[], empty=[], unsettled=[], failed=[])

    with patch.object(TickStore, "sync", return_value=report) as sync_mock:
        code = q_sync_ticks.main(
            ["--symbol", "WDO$N", "--start", "2026-10-05", "--end", "2026-10-06", "--root", str(tmp_path)]
        )

    assert code == 0
    sync_mock.assert_called_once_with(start="2026-10-05", end="2026-10-06", gateway_url=None, gateway_token=None)
    output = capsys.readouterr().out
    assert "stored: 2026-10-05" in output
    assert "No sessions stored yet for this symbol." in output
