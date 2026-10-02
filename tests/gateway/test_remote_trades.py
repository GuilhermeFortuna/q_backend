"""RemoteMt5Client.get_trades against the real gateway module over HTTP (fake MetaTrader5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from q_backend.market_data.clients.remote import RemoteMt5Client
from tests.gateway.conftest import running_gateway_server

_DTYPE = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
    ("last", "f8"),
    ("volume", "f8"),
    ("time_msc", "i8"),
    ("flags", "i4"),
    ("volume_real", "f8"),
]
_START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_START_MSC = int(_START.timestamp() * 1000)


def _ticks(rows):
    return np.array(
        [
            ((_START_MSC + off) // 1000, 1.0, 2.0, 100.0, vol, _START_MSC + off, flags, real)
            for off, vol, real, flags in rows
        ],
        dtype=_DTYPE,
    )


def test_remote_trades_preserve_utc_volume_context_and_flag_eligibility(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _ticks(
        [(0, 3.0, 2.0, 8 | 16), (0, 3.0, 2.0, 8 | 16), (5, 3.0, 2.0, 2 | 4), (9, 1.0, 1.0, 32 | 8)]
    )
    with running_gateway_server(gateway) as base:
        client = RemoteMt5Client(base_url=base)
        result = client.get_trades("WIN$", _START, _START + timedelta(minutes=1))

    assert list(result.columns["time_msc"]) == [_START_MSC, _START_MSC, _START_MSC + 9]
    assert list(result.columns["occurrence"]) == [0, 1, 0]
    assert result.columns["time_msc"].dtype == np.int64
    assert result.columns["raw_flags"].dtype == np.int32
    assert list(result.columns["volume"]) == [3.0, 3.0, 1.0]
    assert list(result.columns["volume_real"]) == [2.0, 2.0, 1.0]
    assert (result.volume_field, result.volume_unit) == ("volume_real", "contracts")
    assert result.availability == "available" and result.range_complete is True
    assert result.covered_from_utc == _START
    assert result.covered_to_utc == _START + timedelta(minutes=1)
    assert result.covered_from_utc.utcoffset() == timedelta(0)


def test_remote_trades_without_volume_real_use_nan_and_raw_volume_field(gateway, fake_mt5):
    legacy = np.array([(_START_MSC // 1000, 1.0, 2.0, 100.0, 4.0, _START_MSC, 8)], dtype=_DTYPE[:-1])
    fake_mt5._state["ticks_source"] = legacy
    with running_gateway_server(gateway) as base:
        result = RemoteMt5Client(base_url=base).get_trades("WIN$", _START, _START + timedelta(seconds=1))

    assert result.volume_field == "volume"
    assert np.isnan(result.columns["volume_real"]).all() and len(result) == 1


def test_remote_trades_surface_truncation_and_outage_honestly(gateway, fake_mt5, monkeypatch):
    monkeypatch.setattr(gateway, "_MAX_TRADE_ROWS", 1)
    fake_mt5._state["ticks_source"] = _ticks([(0, 1.0, 1.0, 8), (1, 1.0, 1.0, 8)])
    with running_gateway_server(gateway) as base:
        client = RemoteMt5Client(base_url=base)
        truncated = client.get_trades("WIN$", _START, _START + timedelta(seconds=1))
        fake_mt5._state["ticks_queue"] = [None]
        outage = client.get_trades("WIN$", _START, _START + timedelta(seconds=1))

    assert truncated.truncated and not truncated.range_complete and len(truncated) == 0
    assert outage.availability == "unavailable" and outage.coverage_reason == "source_error"


def test_remote_trades_map_unknown_symbol_and_unreachable_gateway(gateway, fake_mt5):
    fake_mt5._state["known_symbols"].clear()
    with running_gateway_server(gateway) as base:
        unknown = RemoteMt5Client(base_url=base).get_trades("NOPE", _START, _START + timedelta(seconds=1))
    assert unknown.availability == "unavailable" and unknown.coverage_reason == "symbol_not_found"

    with pytest.raises(ConnectionError):
        RemoteMt5Client(base_url="http://127.0.0.1:9", timeout=0.5).get_trades(
            "WIN$", _START, _START + timedelta(seconds=1)
        )


def test_remote_trades_require_timezone_aware_ranges():
    client = RemoteMt5Client(base_url="http://127.0.0.1:9")
    with pytest.raises(ValueError):
        client.get_trades("WIN$", datetime(2026, 10, 1, 12, 0), _START)


def test_remote_trades_treat_a_gateway_without_the_endpoint_as_an_outage(gateway, fake_mt5, monkeypatch):
    monkeypatch.delitem(gateway._GatewayHandler._ROUTES, "/v1/trades")
    with running_gateway_server(gateway) as base:
        with pytest.raises(ConnectionError, match="redeploy"):
            RemoteMt5Client(base_url=base).get_trades("WIN$", _START, _START + timedelta(seconds=1))
