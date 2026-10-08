"""Tests for q_backend.research.tick_store."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import NoMarketDataError, TickStore, load_ticks, resample_ticks
from q_backend.research.tick_store import TickSyncReport, _slug_symbol, _write_day_atomic


def _ms_at(day: date, hour: int, minute: int, second: int = 0, msec: int = 0) -> int:
    dt = datetime(day.year, day.month, day.day, hour, minute, second, msec * 1000, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _columnar_for_day(day: date, rows: list[tuple[int, float, int]]) -> dict[str, np.ndarray]:
    """rows: (minute_offset, last, flags) within the day."""
    times = [_ms_at(day, 10, minute) for minute, _, _ in rows]
    lasts = [last for _, last, _ in rows]
    flags = [flag for _, _, flag in rows]
    count = len(rows)
    return {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": np.full(count, 99.0),
        "ask": np.full(count, 101.0),
        "last": np.asarray(lasts, dtype=np.float64),
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.asarray(flags, dtype=np.int32),
    }


def _frozen_now() -> datetime:
    return datetime(2026, 10, 8, 15, 0, tzinfo=BRASILIA_TZ)


class ScriptedGateway:
    def __init__(self, script: dict[date, list[int | Exception]]) -> None:
        self.script = script
        self.calls: list[date] = []
        self._call_index: dict[date, int] = {}

    def get_ticks_columnar(
        self, symbol: str, start: datetime, end: datetime, **kwargs: object
    ) -> dict[str, np.ndarray]:
        day = start.date()
        self.calls.append(day)
        entry = self.script[day]
        idx = self._call_index.get(day, 0)
        self._call_index[day] = idx + 1
        if idx >= len(entry):
            return _columnar_for_day(day, [])
        value = entry[idx]
        if isinstance(value, Exception):
            raise value
        count = value
        rows = [(i, 100.0 + i, 32) for i in range(count)]
        return _columnar_for_day(day, rows)


@patch("q_backend.research.tick_store._exchange_now", return_value=_frozen_now())
@patch("q_backend.research.tick_store.RemoteMt5Client")
@patch("q_backend.research.tick_store.resolve_gateway_url", return_value="http://gw.test")
@patch("q_backend.research.tick_store.resolve_gateway_token", return_value=None)
def test_sync_stores_weekdays_and_reports_states(
    _token: object,
    _url: object,
    client_cls: MagicMock,
    _now: object,
    tmp_path,
) -> None:
    # Mon 5 .. Wed 7 Oct 2026; today is Thu 8 — current day skipped; weekend skipped in range.
    mon = date(2026, 10, 5)
    tue = date(2026, 10, 6)
    wed = date(2026, 10, 7)
    thu = date(2026, 10, 8)
    gateway = ScriptedGateway(
        {
            mon: [3, 3],
            tue: [0, 0, 0, 0],
            wed: [2, 5, 2, 5],
            thu: [10, 10],
        }
    )
    client_cls.return_value.is_supported.return_value = True
    client_cls.return_value.get_ticks_columnar.side_effect = gateway.get_ticks_columnar

    store = TickStore("WDO$N", root=tmp_path)
    report = store.sync(start="2026-10-05", end="2026-10-08")

    assert report.stored == [mon]
    assert report.already_present == []
    assert report.empty == [tue]
    assert report.unsettled == [wed]
    assert report.failed == []
    assert thu not in gateway.calls
    slug = _slug_symbol("WDO$N")
    assert (tmp_path / slug / "2026-10-05.parquet").is_file()
    assert not (tmp_path / slug / "2026-10-06.parquet").exists()
    assert not (tmp_path / slug / "2026-10-07.parquet").exists()

    calls_before = len(gateway.calls)
    report2 = store.sync(start="2026-10-05", end="2026-10-07")
    assert report2.already_present == [mon]
    assert report2.stored == []
    assert report2.empty == [tue]
    assert report2.unsettled == [wed]
    assert len(gateway.calls) == calls_before + 4


@patch("q_backend.research.tick_store._exchange_now", return_value=_frozen_now())
@patch("q_backend.research.tick_store.RemoteMt5Client")
@patch("q_backend.research.tick_store.resolve_gateway_url", return_value="http://gw.test")
def test_sync_reports_failed_day_and_continues(
    _url: object,
    client_cls: MagicMock,
    _now: object,
    tmp_path,
) -> None:
    mon = date(2026, 10, 5)
    tue = date(2026, 10, 6)
    gateway = ScriptedGateway({mon: [ConnectionError("down")], tue: [1, 1]})
    client_cls.return_value.is_supported.return_value = True
    client_cls.return_value.get_ticks_columnar.side_effect = gateway.get_ticks_columnar

    store = TickStore("WIN$", root=tmp_path)
    report = store.sync(start="2026-10-05", end="2026-10-06")
    assert report.failed == [mon]
    assert report.stored == [tue]


def test_interrupted_write_leaves_no_final_day_file(tmp_path) -> None:
    day = date(2026, 10, 5)
    path = tmp_path / "ticks" / "2026-10-05.parquet"
    arrays = _columnar_for_day(day, [(0, 100.0, 32), (1, 101.0, 32)])
    with patch.object(pq, "write_table", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError, match="disk full"):
            _write_day_atomic(path, arrays)
    assert not path.exists()
    assert list(path.parent.glob("*.parquet*")) == []


def test_tick_store_construction_is_inert(tmp_path) -> None:
    root = tmp_path / "store"
    store = TickStore("WDO$N", root=root)
    assert store.symbol == "WDO$N"
    assert not root.exists()


def test_ticks_matches_load_ticks_frame(tmp_path) -> None:
    day = date(2026, 10, 5)
    arrays = _columnar_for_day(
        day,
        [
            (0, 11.0, 1),
            (0, 12.0, 2),
            (1, 10.0, 0),
        ],
    )
    store = TickStore("WIN$", root=tmp_path)
    store_dir = tmp_path / _slug_symbol("WIN$")
    store_dir.mkdir(parents=True)
    _write_day_atomic(store_dir / "2026-10-05.parquet", arrays)

    with (
        patch("q_backend.research.data.RemoteMt5Client") as client_cls,
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
        patch("q_backend.research.data._exchange_now", return_value=_frozen_now()),
    ):
        client_cls.return_value.is_supported.return_value = True
        client_cls.return_value.get_ticks_columnar.return_value = arrays
        expected = load_ticks("WIN$", start="2026-10-05T10:00:00", end="2026-10-05T10:02:00")

    actual = store.ticks(start="2026-10-05T10:00:00", end="2026-10-05T10:02:00")
    pd.testing.assert_frame_equal(actual, expected)
    assert actual.attrs["q_research"]["source"] == "tick_store"

    with pytest.raises(NoMarketDataError) as exc:
        store.ticks(start="2026-09-01", end="2026-09-02")
    assert exc.value.source == "tick_store"


def _write_session_fixture(tmp_path) -> None:
    day1 = date(2026, 10, 5)
    day2 = date(2026, 10, 6)
    slug_dir = tmp_path / _slug_symbol("WDO$N")
    slug_dir.mkdir(parents=True)
    # Session 1: trades at 10:00 and 10:10 (gap in M10 middle bar)
    _write_day_atomic(
        slug_dir / "2026-10-05.parquet",
        _columnar_for_day(
            day1,
            [
                (0, 100.0, 32),
                (5, 0.0, 2),
                (10, 102.0, 32),
            ],
        ),
    )
    # Session 2: single trade
    _write_day_atomic(
        slug_dir / "2026-10-06.parquet",
        _columnar_for_day(day2, [(0, 99.0, 64)]),
    )


def test_bars_m10_matches_resample_ticks_per_session(tmp_path) -> None:
    store = TickStore("WDO$N", root=tmp_path)
    _write_session_fixture(tmp_path)

    actual = store.bars("M10", start="2026-10-05", end="2026-10-06T23:59:59")

    expected_parts: list[pd.DataFrame] = []
    for day in (date(2026, 10, 5), date(2026, 10, 6)):
        arrays = _read_day(tmp_path, day)
        from q_backend.research.data import _ticks_frame_from_columnar

        ticks = _ticks_frame_from_columnar(arrays)
        bars = resample_ticks(ticks, timeframe="M10")
        bars = bars[bars["tick_volume"] > 0]
        bars["spread"] = np.nan
        bars["real_volume"] = 0.0
        flags = arrays["flags"]
        vol = arrays["volume"]
        # real_volume is session-specific; compare OHLC + tick_volume only for resample parity
        expected_parts.append(bars)

    expected = pd.concat(expected_parts).sort_index()
    pd.testing.assert_frame_equal(
        actual[["open", "high", "low", "close", "tick_volume"]],
        expected[["open", "high", "low", "close", "tick_volume"]],
    )
    assert actual.attrs["q_research"]["source"] == "tick_store"

    cached = store.bars("M10", start="2026-10-05", end="2026-10-06T23:59:59")
    pd.testing.assert_frame_equal(actual, cached)
    assert (tmp_path / _slug_symbol("WDO$N") / "bars_M1" / "2026-10-05.parquet").is_file()


def _read_day(tmp_path, day: date) -> dict[str, np.ndarray]:
    from q_backend.research.tick_store import _read_day_columnar

    return _read_day_columnar(tmp_path / _slug_symbol("WDO$N") / f"{day.isoformat()}.parquet")


def test_trade_prices_half_open_and_session_cache(tmp_path) -> None:
    day = date(2026, 10, 5)
    store = TickStore("WDO$N", root=tmp_path)
    slug_dir = tmp_path / _slug_symbol("WDO$N")
    slug_dir.mkdir(parents=True)
    arrays = _columnar_for_day(
        day,
        [
            (0, 100.0, 32),
            (5, 101.0, 32),
            (10, 102.0, 32),
        ],
    )
    _write_day_atomic(slug_dir / "2026-10-05.parquet", arrays)

    start = "2026-10-05T10:05:00"
    end = "2026-10-05T10:10:00"
    from q_backend.research.tick_store import _read_day_columnar

    with patch("q_backend.research.tick_store._read_day_columnar", side_effect=_read_day_columnar) as read_mock:
        times1, prices1 = store.trade_prices(start, end)
        times2, prices2 = store.trade_prices(start, end)
    assert prices1.tolist() == [101.0]
    assert prices2.tolist() == [101.0]
    assert read_mock.call_count == 1


def test_bars_rejects_w1() -> None:
    store = TickStore("WIN$", root="/tmp/unused")
    with pytest.raises(ValueError, match="W1"):
        store.bars("W1", start="2026-10-01")


def test_bars_sync_true_invokes_sync(tmp_path) -> None:
    store = TickStore("WDO$N", root=tmp_path)
    _write_session_fixture(tmp_path)
    empty_report = TickSyncReport(stored=[], already_present=[], empty=[], unsettled=[], failed=[])

    with patch.object(store, "sync", return_value=empty_report) as sync_mock:
        store.bars("M10", start="2026-10-05", end="2026-10-06", sync=True)

    sync_mock.assert_called_once_with(start="2026-10-05", end="2026-10-06", gateway_url=None, gateway_token=None)


def test_bars_without_sessions_suggests_sync(tmp_path) -> None:
    store = TickStore("WDO$N", root=tmp_path)
    with pytest.raises(NoMarketDataError, match="q-sync-ticks") as exc:
        store.bars("M10", start="2026-10-05")
    assert "sync=True" in str(exc.value)
