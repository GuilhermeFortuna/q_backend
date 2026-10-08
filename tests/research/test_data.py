"""Tests for q_backend.research load_bars."""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import numpy as np

from q_backend.market_data.models import OHLCV
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import AdjustedSeriesWarning, NoMarketDataError, load_bars, load_ticks, resample_ticks
from q_backend.research.frame import (
    drop_forming_bar,
    is_bar_complete,
    normalize_timeframe,
    parse_bounds,
    validate_bars_frame,
)


def _tick_arrays(times: list[int], lasts: list[float]) -> dict[str, np.ndarray]:
    count = len(times)
    return {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": np.full(count, 99.0),
        "ask": np.full(count, 101.0),
        "last": np.asarray(lasts, dtype=np.float64),
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.arange(count, dtype=np.int32),
    }


def test_load_ticks_returns_ordered_brasilia_frame_and_forwards_flags() -> None:
    # MT5 time_msc stores broker wall time as UTC-shaped epoch milliseconds.
    start_ms = int(datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ticks_columnar.return_value = _tick_arrays(
        [start_ms + 2, start_ms + 1, start_ms + 1], [10.0, 11.0, 12.0]
    )
    with (
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value="secret"),
    ):
        frame = load_ticks("WIN$", start="2026-06-02T10:00:00", end="2026-06-02T10:01:00", flags=2)
    assert list(frame.columns) == ["bid", "ask", "last", "volume", "flags"]
    assert str(frame.index.tz) == "America/Sao_Paulo"
    assert frame.index.is_monotonic_increasing
    assert frame.index[0].strftime("%H:%M:%S.%f") == "10:00:00.001000"
    assert frame.index[0] == frame.index[1]
    assert frame["last"].tolist() == [11.0, 12.0, 10.0]
    assert frame["flags"].tolist() == ["undocumented bits (1)", "bid update", "no flags"]
    client.get_ticks_columnar.assert_called_once()
    assert client.get_ticks_columnar.call_args.kwargs["flags"] == 2
    assert client.get_ticks_columnar.call_args.kwargs["use_cache"] is False


def test_load_ticks_decodes_combined_flags_without_losing_unknown_bits() -> None:
    start_ms = int(datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    raw_flags = [0, 2, 4, 8, 16, 32, 64, 6, 24, 1080, 1336, 1368, 1400, 256, 2048, 1336]
    arrays = _tick_arrays([start_ms + i for i in range(len(raw_flags))], [100.0] * len(raw_flags))
    arrays["flags"] = np.asarray(raw_flags, dtype=np.int32)
    client = MagicMock()
    client.get_ticks_columnar.return_value = arrays
    with (
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        frame = load_ticks("WDO$N", start="2026-06-02T10:00:00", end="2026-06-02T10:01:00")
    assert frame["flags"].tolist() == [
        "no flags",
        "bid update",
        "ask update",
        "last-price update",
        "volume update",
        "buy trade",
        "sell trade",
        "bid update | ask update",
        "last-price update | volume update",
        "last-price update | volume update | buy trade | undocumented bits (1024)",
        "last-price update | volume update | buy trade | undocumented bits (1280)",
        "last-price update | volume update | sell trade | undocumented bits (1280)",
        "last-price update | volume update | buy trade | sell trade | undocumented bits (1280)",
        "undocumented bits (256)",
        "undocumented bits (2048)",
        "last-price update | volume update | buy trade | undocumented bits (1280)",
    ]
    np.testing.assert_array_equal(arrays["flags"], raw_flags)


def test_resample_ticks_aggregates_trades_and_forward_fills_empty_bars() -> None:
    idx = pd.DatetimeIndex(
        ["2026-06-02 10:00:05", "2026-06-02 10:00:40", "2026-06-02 10:02:05"],
        tz=BRASILIA_TZ,
        name="time",
    )
    ticks = pd.DataFrame({"last": [100.0, 102.0, 99.0]}, index=idx)
    bars = resample_ticks(ticks, timeframe="M1")
    assert bars.index.strftime("%H:%M").tolist() == ["10:00", "10:01", "10:02"]
    assert bars[["open", "high", "low", "close"]].values.tolist() == [
        [100.0, 102.0, 100.0, 102.0],
        [102.0, 102.0, 102.0, 102.0],
        [99.0, 99.0, 99.0, 99.0],
    ]
    assert bars["tick_volume"].tolist() == [2, 0, 1]


def test_resample_ticks_ignores_quote_only_updates() -> None:
    idx = pd.DatetimeIndex(["2026-06-02 10:00:05", "2026-06-02 10:00:40"], tz=BRASILIA_TZ, name="time")
    ticks = pd.DataFrame({"last": [100.0, 0.0]}, index=idx)
    bars = resample_ticks(ticks, timeframe="M1")
    assert bars["close"].tolist() == [100.0]
    assert bars["tick_volume"].tolist() == [1]


def _ohlcv_at(time: str, *, close: float = 100.5) -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime.fromisoformat(time),
            open=close - 0.5,
            high=close + 0.5,
            low=close - 1.0,
            close=close,
            tick_volume=10,
            spread=1,
            real_volume=0,
        )
    ]


def _ohlcv_series(times: list[str], *, close: float = 100.0) -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime.fromisoformat(t),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            tick_volume=10,
            spread=1,
            real_volume=0,
        )
        for t in times
    ]


@contextmanager
def _patch_load_bars(client: MagicMock, frozen: datetime | None = None):
    frozen = frozen or datetime(2026, 6, 2, 12, 0, tzinfo=BRASILIA_TZ)
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        yield


def _frame_at(time: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex([time], tz=BRASILIA_TZ, name="time")
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "tick_volume": [10],
            "spread": [1.0],
            "real_volume": [0.0],
        },
        index=idx,
    )


def test_load_bars_on_grid_records_tick_metadata_without_warning() -> None:
    client = MagicMock()
    client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00", close=5400.5)
    client.get_symbol_info.return_value = {"trade_tick_size": 0.5}
    with _patch_load_bars(client):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            frame = load_bars("WDO$N", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert not any(item.category is AdjustedSeriesWarning for item in caught)
    meta = frame.attrs["q_research"]
    assert meta["tick_size"] == 0.5
    assert meta["off_tick_share"] == 0.0
    client.get_symbol_info.assert_called_once_with("WDO$N")


def test_load_bars_off_grid_above_threshold_warns_once() -> None:
    client = MagicMock()
    client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00", close=100.33)
    client.get_symbol_info.return_value = {"trade_tick_size": 0.5}
    with _patch_load_bars(client):
        with pytest.warns(AdjustedSeriesWarning, match="WDO\\$") as record:
            frame = load_bars("WDO$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert len(record) == 1
    assert record[0].filename == __file__
    assert frame.attrs["q_research"]["off_tick_share"] == 1.0


def test_load_bars_off_grid_at_threshold_does_not_warn() -> None:
    # 100 OHLC values; one off-grid close -> share == 0.01
    times = [f"2026-06-02T10:{i:02d}:00" for i in range(25)]
    bars = _ohlcv_series(times, close=100.0)
    bars[-1] = OHLCV(
        time=datetime.fromisoformat(times[-1]),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.33,
        tick_volume=10,
        spread=1,
        real_volume=0,
    )
    client = MagicMock()
    client.get_ohlcv.return_value = bars
    client.get_symbol_info.return_value = {"trade_tick_size": 0.5}
    with _patch_load_bars(client):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            frame = load_bars("WDO$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert not any(item.category is AdjustedSeriesWarning for item in caught)
    assert frame.attrs["q_research"]["off_tick_share"] == pytest.approx(0.01)


def test_load_bars_symbol_info_failures_skip_tick_metadata() -> None:
    frozen = datetime(2026, 6, 2, 12, 0, tzinfo=BRASILIA_TZ)
    baseline_client = MagicMock()
    baseline_client.is_supported.return_value = True
    baseline_client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00")
    baseline_client.get_symbol_info.return_value = None
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=baseline_client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            baseline = load_bars("WIN$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert not any(item.category is AdjustedSeriesWarning for item in caught)
    assert "tick_size" not in baseline.attrs["q_research"]
    assert "off_tick_share" not in baseline.attrs["q_research"]

    failure_modes = [
        {"get_symbol_info": None},
        {"get_symbol_info": {}},
        {"get_symbol_info": {"trade_tick_size": 0}},
        {"get_symbol_info": {"trade_tick_size": float("nan")}},
        {"get_symbol_info": ConnectionError("down")},
    ]
    for mode in failure_modes:
        client = MagicMock()
        client.is_supported.return_value = True
        client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00")
        if isinstance(mode["get_symbol_info"], Exception):
            client.get_symbol_info.side_effect = mode["get_symbol_info"]
        else:
            client.get_symbol_info.return_value = mode["get_symbol_info"]
        with _patch_load_bars(client, frozen=frozen):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                frame = load_bars("WIN$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
        assert not any(item.category is AdjustedSeriesWarning for item in caught)
        assert "tick_size" not in frame.attrs["q_research"]
        assert "off_tick_share" not in frame.attrs["q_research"]
        pd.testing.assert_frame_equal(frame, baseline)


def test_load_bars_schema_metadata_and_mt5_source() -> None:
    frozen = datetime(2026, 6, 2, 12, 0, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00")
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value="secret"),
    ):
        frame = load_bars("WIN$", timeframe="m5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert frame.index.name == "time"
    assert str(frame.index.tz) == "America/Sao_Paulo"
    assert list(frame.columns) == [
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "spread",
        "real_volume",
    ]
    meta = frame.attrs["q_research"]
    assert meta["symbol"] == "WIN$"
    assert meta["timeframe"] == "M5"
    assert meta["source"] == "mt5"
    assert "dataset_id" not in meta
    client.get_ohlcv.assert_called_once()


def test_two_calls_issue_two_gateway_requests() -> None:
    frozen = datetime(2026, 6, 2, 12, 0, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00")
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        load_bars("WIN$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
        load_bars("WIN$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert client.get_ohlcv.call_count == 2


def test_omitted_end_uses_captured_now() -> None:
    frozen = datetime(2026, 6, 2, 12, 0, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = _ohlcv_at("2026-06-02T10:00:00")
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        load_bars("WIN$", timeframe="M5", start="2026-06-02T09:00:00")
    args = client.get_ohlcv.call_args[0]
    end_naive = args[3]
    assert end_naive == frozen.replace(tzinfo=None)


def test_utc_and_brasilia_bounds_select_same_rows() -> None:
    frozen = datetime(2018, 10, 20, 13, 30, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = [
        OHLCV(
            time=datetime(2018, 10, 20, 9, 0),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            tick_volume=10,
            spread=1,
            real_volume=0,
        )
    ]
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        local = load_bars(
            "WIN$",
            timeframe="M15",
            start="2018-10-20T09:00:00-03:00",
            end="2018-10-20T10:30:00-03:00",
        )
        client.get_ohlcv.reset_mock()
        client.get_ohlcv.return_value = client.get_ohlcv.return_value
        utc = load_bars(
            "WIN$",
            timeframe="M15",
            start="2018-10-20T12:00:00Z",
            end="2018-10-20T13:30:00Z",
        )
    pd.testing.assert_frame_equal(local, utc)


def test_missing_gateway_url_raises() -> None:
    with patch("q_backend.research.data.resolve_gateway_url", return_value=None):
        with pytest.raises(ValueError, match="gateway URL"):
            load_bars("WIN$", timeframe="M5", start="2026-01-01", end="2026-01-02")


def test_gateway_failure_propagates() -> None:
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.side_effect = ConnectionError("gateway down")
    with (
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        with pytest.raises(ConnectionError, match="gateway down"):
            load_bars("WIN$", timeframe="M5", start="2026-01-01", end="2026-01-02")


def test_empty_history_raises_no_market_data() -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = []
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        with pytest.raises(NoMarketDataError, match="mt5"):
            load_bars("WIN$", timeframe="M5", start="2026-01-01", end="2026-01-02")


def test_forming_bar_excluded_with_frozen_clock() -> None:
    open_time = datetime(2026, 6, 2, 10, 0, tzinfo=BRASILIA_TZ)
    frozen = open_time + timedelta(minutes=7)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = [
        OHLCV(
            time=datetime(2026, 6, 2, 10, 0),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            tick_volume=10,
            spread=1,
            real_volume=0,
        ),
        OHLCV(
            time=datetime(2026, 6, 2, 10, 5),
            open=2.0,
            high=2.1,
            low=1.9,
            close=2.05,
            tick_volume=11,
            spread=1,
            real_volume=0,
        ),
    ]
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("q_backend.research.data.resolve_gateway_token", return_value=None),
    ):
        frame = load_bars("WIN$", timeframe="M5", start="2026-06-02T10:00:00", end="2026-06-02T11:00:00")
    assert len(frame) == 1


def test_explicit_gateway_overrides_do_not_mutate_environment() -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = _ohlcv_at("2026-01-01T10:00:00")
    env_before = dict(__import__("os").environ)
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client) as client_ctor,
    ):
        load_bars(
            "WIN$",
            timeframe="M5",
            start="2026-01-01",
            end="2026-01-01T12:00:00",
            gateway_url="http://override.test",
            gateway_token="tok",
        )
    assert __import__("os").environ == env_before
    client_ctor.assert_called_once_with(base_url="http://override.test", token="tok")


def test_no_sqlalchemy_engine_on_load() -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    client = MagicMock()
    client.is_supported.return_value = True
    client.get_ohlcv.return_value = _ohlcv_at("2026-01-01T10:00:00")
    with (
        patch("q_backend.research.data._exchange_now", return_value=frozen),
        patch("q_backend.research.data.RemoteMt5Client", return_value=client),
        patch("q_backend.research.data.resolve_gateway_url", return_value="http://gw.test"),
        patch("sqlalchemy.create_engine", side_effect=AssertionError("no database engine")),
    ):
        load_bars("WIN$", timeframe="M5", start="2026-01-01", end="2026-01-01T12:00:00")


def test_invalid_prices_and_duplicates_raise() -> None:
    idx = pd.DatetimeIndex(["2024-01-01"], tz=BRASILIA_TZ, name="time")
    bad_ohlc = pd.DataFrame(
        {
            "open": [10.0],
            "high": [9.0],
            "low": [8.0],
            "close": [9.5],
            "tick_volume": [1],
            "spread": [1.0],
            "real_volume": [1.0],
        },
        index=idx,
    )
    with pytest.raises(ValueError, match="ordering"):
        validate_bars_frame(bad_ohlc)

    dup_idx = pd.DatetimeIndex(["2024-01-01", "2024-01-01"], tz=BRASILIA_TZ, name="time")
    dup = pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "high": [11.0, 11.0],
            "low": [9.0, 9.0],
            "close": [10.5, 10.5],
            "tick_volume": [1, 2],
            "spread": [1.0, 1.0],
            "real_volume": [1.0, 1.0],
        },
        index=dup_idx,
    )
    with pytest.raises(ValueError, match="Duplicate"):
        validate_bars_frame(dup)


def test_drop_forming_m5_bar() -> None:
    open_time = datetime(2025, 6, 2, 10, 0, tzinfo=BRASILIA_TZ)
    idx = pd.date_range(open_time, periods=3, freq="5min", tz=BRASILIA_TZ, name="time")
    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    now = open_time + timedelta(minutes=12)
    trimmed = drop_forming_bar(frame, "M5", now=now)
    assert len(trimmed) == 2


def test_mn1_calendar_forming_bar() -> None:
    jan_open = datetime(2025, 1, 1, 0, 0, tzinfo=BRASILIA_TZ)
    assert not is_bar_complete(jan_open, "MN1", now=datetime(2025, 1, 15, tzinfo=BRASILIA_TZ))
    assert is_bar_complete(jan_open, "MN1", now=datetime(2025, 2, 1, tzinfo=BRASILIA_TZ))


def test_bounds_validation() -> None:
    with pytest.raises(ValueError, match="start must be"):
        parse_bounds("2024-01-02", "2024-01-01")
    with pytest.raises(ValueError, match="Unknown timeframe"):
        normalize_timeframe("INVALID")


def test_load_bars_multi_page_gateway_range_returns_complete_frame() -> None:
    from tests.market_data.test_remote_client import _fake_gateway, _FakeState, _ohlcv_npz

    # MT5 epoch seconds: 10:00, 10:01, 10:02, 10:03 UTC-shaped wall clock
    t0 = int(datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc).timestamp())
    page1_times = [t0, t0 + 60]
    page2_times = [t0 + 120, t0 + 180]
    p1 = _ohlcv_npz(page1_times, metadata={"truncated": True, "max_bars": 2})
    p2 = _ohlcv_npz(page2_times, metadata={"truncated": False, "max_bars": 2})

    state = _FakeState()
    state.ohlcv_queue = [p1, p2]

    frozen = datetime(2026, 6, 2, 11, 0, tzinfo=BRASILIA_TZ)
    with _fake_gateway(state) as (base_url, st):
        with patch("q_backend.research.data._exchange_now", return_value=frozen):
            frame = load_bars(
                "WIN$",
                timeframe="M1",
                start="2026-06-02T10:00:00",
                end="2026-06-02T10:04:00",
                gateway_url=base_url,
            )

    ohlcv_paths = [p for p in st.paths if "/v1/ohlcv?" in p]
    assert len(ohlcv_paths) == 2
    assert len(frame) == 4
    assert frame.index.is_monotonic_increasing
    assert frame.index.is_unique
    assert frame.index[0] == pd.Timestamp("2026-06-02 10:00:00", tz=BRASILIA_TZ)
    assert frame.index[-1] == pd.Timestamp("2026-06-02 10:03:00", tz=BRASILIA_TZ)
    assert frame.attrs["q_research"]["symbol"] == "WIN$"
    assert frame.attrs["q_research"]["timeframe"] == "M1"
