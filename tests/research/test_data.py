"""Tests for q_backend.research load_bars."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from q_backend.market_data.models import OHLCV
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import NoMarketDataError, load_bars
from q_backend.research.frame import (
    drop_forming_bar,
    is_bar_complete,
    normalize_timeframe,
    parse_bounds,
    validate_bars_frame,
)


def _ohlcv_at(time: str) -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime.fromisoformat(time),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            tick_volume=10,
            spread=1,
            real_volume=0,
        )
    ]


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
