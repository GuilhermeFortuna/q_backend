"""HTTP round-trip tests for the MT5 remote data gateway (WO183).

The gateway lives outside ``src/`` and must never import ``q_backend``. We load it
straight from its file path with ``importlib`` after installing a fake ``MetaTrader5``
module into ``sys.modules`` (mirroring the ``docker/metatrader5-stub`` pattern), then
exercise every endpoint over real HTTP on an ephemeral port.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import types
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pytest

_GATEWAY_PATH = (
    Path(__file__).resolve().parents[2] / "gateway" / "mt5_gateway.py"
)

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

_SymbolInfo = namedtuple(
    "SymbolInfo", ["name", "description", "digits", "point", "trade_mode"]
)
_SymbolSearch = namedtuple("SymbolSearch", ["name", "description", "path", "custom"])
_TerminalInfo = namedtuple("TerminalInfo", ["build"])


def _make_fake_mt5() -> types.ModuleType:
    """Build a controllable fake MetaTrader5 module (read-only surface only)."""
    m = types.ModuleType("MetaTrader5")

    # Timeframe + tick-flag constants (values mirror the real bindings / stub).
    m.TIMEFRAME_M1 = 1
    m.TIMEFRAME_M2 = 2
    m.TIMEFRAME_M3 = 3
    m.TIMEFRAME_M4 = 4
    m.TIMEFRAME_M5 = 5
    m.TIMEFRAME_M6 = 6
    m.TIMEFRAME_M10 = 10
    m.TIMEFRAME_M12 = 12
    m.TIMEFRAME_M15 = 15
    m.TIMEFRAME_M20 = 20
    m.TIMEFRAME_M30 = 30
    m.TIMEFRAME_H1 = 16385
    m.TIMEFRAME_H2 = 16386
    m.TIMEFRAME_H3 = 16387
    m.TIMEFRAME_H4 = 16388
    m.TIMEFRAME_H6 = 16390
    m.TIMEFRAME_H8 = 16392
    m.TIMEFRAME_H12 = 16396
    m.TIMEFRAME_D1 = 16408
    m.TIMEFRAME_W1 = 32769
    m.TIMEFRAME_MN1 = 49153
    m.COPY_TICKS_ALL = 1
    m.COPY_TICKS_TRADE = 2

    state = {
        "init_ok": True,
        "known_symbols": {"WIN$"},
        "symbol_info": {
            "WIN$": _SymbolInfo("WIN$", "Mini Ibovespa", 0, 1.0, 4),
        },
        "search": [
            _SymbolSearch("WIN$", "Mini Ibovespa", "Futures\\WIN$", False),
            _SymbolSearch("WINQ26", "Mini Ibovespa Aug26", "Futures\\WINQ26", False),
        ],
        "rates_queue": [],
        "rates_default": np.empty(0, dtype=_RATE_DTYPE),
        "rates_from_pos": None,
        "rates_from": None,
        "ticks_queue": [],
        "ticks_default": np.empty(0, dtype=_TICK_DTYPE),
        "last_flags": None,
        "build": 4200,
    }
    m._state = state

    def initialize(**_kwargs):
        return state["init_ok"]

    def shutdown():
        return None

    def last_error():
        return (-1, "fake error")

    def symbol_select(symbol, _enable):
        return symbol in state["known_symbols"]

    def symbol_info(symbol):
        return state["symbol_info"].get(symbol)

    def symbols_get(_pattern=None):
        return state["search"]

    def terminal_info():
        return _TerminalInfo(build=state["build"])

    def copy_rates_range(_symbol, _timeframe, _date_from, _date_to):
        if state["rates_queue"]:
            return state["rates_queue"].pop(0)
        return state["rates_default"]

    def copy_rates_from_pos(_symbol, _timeframe, _pos, _count):
        return state["rates_from_pos"]

    def copy_rates_from(_symbol, _timeframe, _date_from, _count):
        return state["rates_from"]

    def copy_ticks_range(_symbol, _date_from, _date_to, flags):
        state["last_flags"] = flags
        if state["ticks_queue"]:
            return state["ticks_queue"].pop(0)
        return state["ticks_default"]

    m.initialize = initialize
    m.shutdown = shutdown
    m.last_error = last_error
    m.symbol_select = symbol_select
    m.symbol_info = symbol_info
    m.symbols_get = symbols_get
    m.terminal_info = terminal_info
    m.copy_rates_range = copy_rates_range
    m.copy_rates_from_pos = copy_rates_from_pos
    m.copy_rates_from = copy_rates_from
    m.copy_ticks_range = copy_ticks_range
    return m


def _load_gateway(fake: types.ModuleType):
    """Load mt5_gateway.py from file with the fake MetaTrader5 bound at import."""
    saved = sys.modules.get("MetaTrader5")
    sys.modules["MetaTrader5"] = fake
    try:
        spec = importlib.util.spec_from_file_location(
            "mt5_gateway_under_test", _GATEWAY_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is not None:
            sys.modules["MetaTrader5"] = saved
        else:
            sys.modules.pop("MetaTrader5", None)


@pytest.fixture
def fake_mt5() -> types.ModuleType:
    return _make_fake_mt5()


@pytest.fixture
def gateway(fake_mt5):
    return _load_gateway(fake_mt5)


@contextmanager
def _running_server(gateway_module, token: str | None = None):
    server = gateway_module.build_server("127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str, params=None, headers=None):
    url = base_url + path
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_health_reports_schema_and_connection(gateway, fake_mt5):
    with _running_server(gateway) as base:
        status, _headers, body = _get(base, "/v1/health")

    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["schema_version"] == "1.0"
    assert payload["mt5_connected"] is True
    assert payload["terminal_build"] == 4200


def test_health_reports_disconnected_when_initialize_fails(gateway, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with _running_server(gateway) as base:
        status, _headers, body = _get(base, "/v1/health")

    assert status == 200  # health is always 200
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

    with _running_server(gateway) as base:
        status, headers, body = _get(
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
        (base_epoch + i, 130000.0 + i, 130010.0 + i, 130005.0 + i, 3.0, (base_epoch + i) * 1000, 6)
        for i in range(3)
    ]
    ticks = np.array(rows, dtype=_TICK_DTYPE)
    fake_mt5._state["ticks_queue"] = [ticks]

    with _running_server(gateway) as base:
        status, _headers, body = _get(
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
        assert npz["bid"].dtype == np.float64
        assert npz["ask"].dtype == np.float64
        assert npz["last"].dtype == np.float64
        assert npz["volume"].dtype == np.float64
        assert npz["flags"].dtype == np.int32
        assert list(npz["time_msc"]) == [(base_epoch + i) * 1000 for i in range(3)]
        assert list(npz["flags"]) == [6, 6, 6]


def test_ticks_default_flags_all(gateway, fake_mt5):
    with _running_server(gateway) as base:
        _get(
            base,
            "/v1/ticks",
            {
                "symbol": "WIN$",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert fake_mt5._state["last_flags"] == fake_mt5.COPY_TICKS_ALL


def test_ticks_empty_result_has_columnar_shape(gateway, fake_mt5):
    with _running_server(gateway) as base:
        status, _headers, body = _get(
            base,
            "/v1/ticks",
            {
                "symbol": "WIN$",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:05:00",
            },
        )

    assert status == 200
    with np.load(io.BytesIO(body)) as npz:
        assert set(npz.files) == {"time_msc", "bid", "ask", "last", "volume", "flags"}
        assert len(npz["time_msc"]) == 0
        assert npz["time_msc"].dtype == np.int64
        assert npz["flags"].dtype == np.int32


def test_invalid_timeframe_returns_400(gateway, fake_mt5):
    with _running_server(gateway) as base:
        status, _headers, body = _get(
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
    with _running_server(gateway) as base:
        status, _headers, body = _get(
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
    with _running_server(gateway) as base:
        status, _headers, body = _get(
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
    with _running_server(gateway, token="s3cret") as base:
        no_header, _h1, body1 = _get(base, "/v1/health")
        with_header, _h2, _body2 = _get(
            base, "/v1/health", headers={"X-Gateway-Token": "s3cret"}
        )

    assert no_header == 401
    assert json.loads(body1)["code"] == "unauthorized"
    assert with_header == 200


def test_symbol_info_and_search(gateway, fake_mt5):
    with _running_server(gateway) as base:
        info_status, _h, info_body = _get(
            base, "/v1/symbol_info", {"symbol": "WIN$"}
        )
        search_status, _h2, search_body = _get(
            base, "/v1/symbols/search", {"query": "win"}
        )

    assert info_status == 200
    assert json.loads(info_body)["name"] == "WIN$"

    assert search_status == 200
    names = [row["name"] for row in json.loads(search_body)]
    assert "WIN$" in names


def test_available_range(gateway, fake_mt5):
    latest_epoch = _epoch(datetime(2026, 1, 5, 18, 0, 0))
    earliest_epoch = _epoch(datetime(2020, 1, 2, 9, 0, 0))
    fake_mt5._state["rates_from_pos"] = np.array(
        [(latest_epoch, 1.0, 1.0, 1.0, 1.0, 1, 0, 0)], dtype=_RATE_DTYPE
    )
    fake_mt5._state["rates_from"] = np.array(
        [(earliest_epoch, 1.0, 1.0, 1.0, 1.0, 1, 0, 0)], dtype=_RATE_DTYPE
    )
    # One non-empty count chunk, then empty terminates the bar count loop.
    fake_mt5._state["rates_queue"] = [
        np.array(
            [(earliest_epoch, 1.0, 1.0, 1.0, 1.0, 1, 0, 0)], dtype=_RATE_DTYPE
        )
    ]

    with _running_server(gateway) as base:
        status, _headers, body = _get(
            base,
            "/v1/available_range",
            {"symbol": "WIN$", "timeframe": "D1"},
        )

    assert status == 200
    payload = json.loads(body)
    assert payload["symbol"] == "WIN$"
    assert payload["timeframe"] == "D1"
    assert payload["bar_count"] >= 1
    assert payload["start"].startswith("2020-01-02T09:00:00")
    assert payload["end"].startswith("2026-01-05T18:00:00")
