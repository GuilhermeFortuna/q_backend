"""Worker resolution and progress for research backtests."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research import tick_phase
from q_backend.research.tick_phase import resolve_workers
from tests.research.test_intrabar_exit_hooks import ENTRY, MOVE, NEXT
from tests.research.test_stop_target_backtest import SYMBOL, _m10_frame, _write_session


class EntersOnce(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return TradeOrder.buy() if len(frame) == 1 else None


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


@pytest.mark.parametrize("workers", [0, -1, True, 2.0, "many"])
def test_invalid_workers_are_rejected(root, workers):
    store = _write_session(root, ENTRY + MOVE + NEXT)
    with pytest.raises(ValueError, match="workers"):
        backtest(_m10_frame(store), strategy=EntersOnce(), symbol=SYMBOL, workers=workers)


def test_auto_workers_are_limited_by_cpus_and_free_memory(monkeypatch):
    monkeypatch.setattr(tick_phase.os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 3 << 30)
    assert resolve_workers("auto") == 3
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 64 << 30)
    assert resolve_workers("auto") == 8
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 1 << 20)
    assert resolve_workers("auto") == 1
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: None)
    assert resolve_workers("auto") == 8
    assert resolve_workers(5) == 5


def test_progress_is_silent_off_a_terminal_and_reports_when_requested(root, capsys):
    store = _write_session(root, ENTRY + MOVE + NEXT)
    frame = _m10_frame(store)

    quiet = backtest(frame, strategy=EntersOnce(), symbol=SYMBOL)
    assert capsys.readouterr().err == ""

    shown = backtest(frame, strategy=EntersOnce(), symbol=SYMBOL, progress=True)
    summary = capsys.readouterr().err
    assert "WDO$N M10" in summary
    assert "4 bars, 1 trade" in summary
    pd.testing.assert_frame_equal(shown.trades.drop(columns="trade_id"), quiet.trades.drop(columns="trade_id"))
