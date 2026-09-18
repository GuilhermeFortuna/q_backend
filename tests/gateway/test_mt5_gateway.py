"""HTTP round-trip tests for the MT5 remote data gateway (WO183)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import numpy as np

from tests.gateway.conftest import http_get, running_gateway_server

_RATE_DTYPE = [
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
]

_TICK_DTYPE = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
    ("last", "f8"),
    ("volume", "f8"),
    ("time_msc", "i8"),
    ("flags", "i4"),
]


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def test_health_reports_schema_and_connection(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(base, "/v1/health")

    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["schema_version"] == "1.0"
    assert payload["mt5_connected"] is True
    assert payload["terminal_build"] == 4200


def test_health_reports_disconnected_when_initialize_fails(gateway, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(base, "/v1/health")

    assert status == 200
    payload = json.loads(body)
    assert payload["mt5_connected"] is False
    assert payload["terminal_build"] is None


def test_ohlcv_round_trip_preserves_raw_epochs_and_dtypes(gateway, fake_mt5):
    base_epoch = _epoch(datetime(2026, 1, 5, 9, 0, 0))
    times = [base_epoch, base_epoch + 60, base_epoch + 120]
    rates = np.array(
        [(t, 130000.0, 130100.0, 129900.0, 130050.0, 500, 1, 250) for t in times],
        dtype=_RATE_DTYPE,
    )
    fake_mt5._state["rates_queue"] = [rates]

    with running_gateway_server(gateway) as base:
        status, headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "WIN$",
                "timeframe": "M1",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert status == 200
    assert headers["Content-Type"] == "application/octet-stream"

    with np.load(io.BytesIO(body)) as npz:
        assert list(npz["time"]) == times
        assert npz["time"].dtype == np.int64
        assert npz["open"].dtype == np.float64
        assert npz["tick_volume"].dtype == np.int64
        assert npz["spread"].dtype == np.int64
        assert npz["real_volume"].dtype == np.int64
        np.testing.assert_array_equal(npz["close"], rates["close"])
        np.testing.assert_array_equal(npz["real_volume"], rates["real_volume"].astype(np.int64))


def test_ticks_round_trip_and_flags_trade_mapping(gateway, fake_mt5):
    base_epoch = _epoch(datetime(2026, 1, 5, 9, 0, 0))
    rows = [
        (base_epoch + i, 130000.0 + i, 130010.0 + i, 130005.0 + i, 3.0, (base_epoch + i) * 1000, 6) for i in range(3)
    ]
    ticks = np.array(rows, dtype=_TICK_DTYPE)
    fake_mt5._state["ticks_queue"] = [ticks]

    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(
            base,
            "/v1/ticks",
            {
                "symbol": "WIN$",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
                "flags": "trade",
            },
        )

    assert status == 200
    assert fake_mt5._state["last_flags"] == fake_mt5.COPY_TICKS_TRADE

    with np.load(io.BytesIO(body)) as npz:
        assert npz["time_msc"].dtype == np.int64
        assert list(npz["time_msc"]) == [(base_epoch + i) * 1000 for i in range(3)]


def test_ticks_default_flags_all(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        http_get(
            base,
            "/v1/ticks",
            {
                "symbol": "WIN$",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert fake_mt5._state["last_flags"] == fake_mt5.COPY_TICKS_ALL


def test_invalid_timeframe_returns_400(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "WIN$",
                "timeframe": "ZZ",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert status == 400
    assert json.loads(body)["code"] == "invalid_timeframe"


def test_unknown_symbol_returns_404(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "NOPE",
                "timeframe": "M1",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert status == 404
    assert json.loads(body)["code"] == "symbol_not_found"


def test_mt5_unavailable_returns_503(gateway, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "WIN$",
                "timeframe": "M1",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert status == 503
    assert json.loads(body)["code"] == "mt5_unavailable"


def test_token_required_when_configured(gateway, fake_mt5):
    with running_gateway_server(gateway, token="s3cret") as base:
        no_header, _h1, body1 = http_get(base, "/v1/health")
        with_header, _h2, _body2 = http_get(base, "/v1/health", headers={"X-Gateway-Token": "s3cret"})

    assert no_header == 401
    assert json.loads(body1)["code"] == "unauthorized"
    assert with_header == 200


def test_symbol_info_and_search(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        info_status, _h, info_body = http_get(base, "/v1/symbol_info", {"symbol": "WIN$"})
        search_status, _h2, search_body = http_get(base, "/v1/symbols/search", {"query": "win"})

    assert info_status == 200
    assert json.loads(info_body)["name"] == "WIN$"
    assert search_status == 200
    assert "WIN$" in [row["name"] for row in json.loads(search_body)]
