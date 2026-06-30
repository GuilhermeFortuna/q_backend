"""Tests for completed-bar frame helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from q_backend.execution.bars import drop_forming_bar, is_bar_complete, trim_rolling_window


def test_drop_forming_bar_removes_incomplete_last_row():
    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    index = pd.date_range(base, periods=3, freq="15min")
    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index)
    # 10:30 bar closes at 10:45; now is 10:40 -> still forming
    now = base + timedelta(minutes=40)
    trimmed = drop_forming_bar(frame, "M15", now=now)
    assert len(trimmed) == 2
    assert trimmed.index[-1] == index[1]


def test_is_bar_complete_after_close():
    open_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert not is_bar_complete(
        open_time, "M15", now=open_time + timedelta(minutes=14)
    )
    assert is_bar_complete(
        open_time, "M15", now=open_time + timedelta(minutes=15)
    )


def test_trim_rolling_window_is_bounded():
    frame = pd.DataFrame({"close": range(20)}, index=pd.RangeIndex(20))
    trimmed = trim_rolling_window(frame, 5)
    assert len(trimmed) == 5
    assert trimmed.index[0] == 15
