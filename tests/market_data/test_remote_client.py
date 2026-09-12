"""RemoteMt5Client against an in-process fake gateway (WO184).

The fake gateway is a stdlib ``ThreadingHTTPServer`` on port 0 serving canned JSON and
``.npz`` payloads (built with ``np.savez_compressed``), mirroring the WO183 wire
contract. Requests are counted so health caching can be asserted.
"""

from __future__ import annotations

import io
import logging
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np
import pytest

from q_backend.market_data.clients.base import MarketDataProvider
from q_backend.market_data.clients.metatrader import (
    COPY_TICKS_ALL,
    COPY_TICKS_TRADE,
)
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.timezone import unix_seconds_to_brasilia_naive


def _ohlcv_npz(times, *, include_spread=True, include_real_volume=True) -> bytes:
    n = len(times)
    arrays = {
        "time": np.array(times, dtype=np.int64),
        "open": np.arange(n, dtype=np.float64) + 100.0,
        "high": np.arange(n, dtype=np.float64) + 101.0,
        "low": np.arange(n, dtype=np.float64) + 99.0,
        "close": np.arange(n, dtype=np.float64) + 100.5,
        "tick_volume": np.arange(n, dtype=np.int64) + 10,
    }
    if include_spread:
        arrays["spread"] = np.ones(n, dtype=np.int64)
    if include_real_volume:
        arrays["real_volume"] = np.zeros(n, dtype=np.int64)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def _ticks_npz(time_msc) -> bytes:
    n = len(time_msc)
    arrays = {
        "time_msc": np.array(time_msc, dtype=np.int64),
        "bid": np.arange(n, dtype=np.float64) + 41.0,
        "ask": np.arange(n, dtype=np.float64) + 41.02,
        "last": np.arange(n, dtype=np.float64) + 41.01,
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.full(n, 6, dtype=np.int32),
    }
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def _empty_ticks_npz() -> bytes:
    arrays = {
        "time_msc": np.array([], dtype=np.int64),
        "bid": np.array([], dtype=np.float64),
        "ask": np.array([], dtype=np.float64),
        "last": np.array([], dtype=np.float64),
        "volume": np.array([], dtype=np.float64),
        "flags": np.array([], dtype=np.int32),
    }
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


class _FakeState:
    def __init__(self):
        self.health = {
            "status": "ok",
            "schema_version": "1.0",
            "mt5_connected": True,
            "terminal_build": 4200,
        }
        self.ohlcv_npz = _ohlcv_npz([1_700_000_000])
        self.ohlcv_error = None  # (status, {"error", "code"})
        self.ticks_npz = _empty_ticks_npz()
        self.symbol_info = {"name": "WIN$", "description": "Mini Ibovespa"}
        self.search = [{"name": "WIN$", "description": "Mini Ibovespa"}]
        self.available_range = {
            "symbol": "WIN$",
            "timeframe": "D1",
            "start": "2020-01-02T09:00:00",
            "end": "2026-01-05T18:00:00",
            "bar_count": 1500,
        }
        self.health_count = 0
        self.request_count = 0
        self.last_headers: dict[str, str] = {}
        self.paths: list[str] = []


class _FakeGateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: _FakeState):
        super().__init__(address, _FakeHandler)
        self.state = state


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # silence
        return

    @property
    def state(self) -> _FakeState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self):  # noqa: N802 - stdlib name
        st = self.state
        st.request_count += 1
        st.last_headers = {k: v for k, v in self.headers.items()}
        st.paths.append(self.path)
        path = urlparse(self.path).path

        if path == "/v1/health":
            st.health_count += 1
            self._json(200, st.health)
        elif path == "/v1/ohlcv":
            if st.ohlcv_error is not None:
                self._json(*st.ohlcv_error)
            else:
                self._npz(st.ohlcv_npz)
        elif path == "/v1/ticks":
            self._npz(st.ticks_npz)
        elif path == "/v1/symbol_info":
            if st.symbol_info is None:
                self._json(404, {"error": "unknown", "code": "symbol_not_found"})
            else:
                self._json(200, st.symbol_info)
        elif path == "/v1/symbols/search":
            self._json(200, st.search)
        elif path == "/v1/available_range":
            if st.available_range is None:
                self._json(404, {"error": "no data", "code": "range_unavailable"})
            else:
                self._json(200, st.available_range)
        else:
            self._json(404, {"error": "unknown", "code": "not_found"})

    def _json(self, status, obj):
        import json

        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _npz(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def _fake_gateway(state: _FakeState | None = None):
    state = state or _FakeState()
    server = _FakeGateway(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_satisfies_marketdataprovider_protocol():
    client = RemoteMt5Client(base_url="http://127.0.0.1:9")
    assert isinstance(client, MarketDataProvider)


def test_get_ohlcv_round_trip_naive_brasilia_time():
    epochs = [1_700_000_000, 1_700_000_060, 1_700_000_120]
    state = _FakeState()
    state.ohlcv_npz = _ohlcv_npz(epochs, include_spread=True)

    with _fake_gateway(state) as (base_url, _st):
        client = RemoteMt5Client(base_url=base_url)
        from datetime import datetime

        bars = client.get_ohlcv("WIN$", "M1", datetime(2023, 11, 14), datetime(2023, 11, 15))

    assert len(bars) == 3
    for bar, epoch in zip(bars, epochs):
        assert bar.time == unix_seconds_to_brasilia_naive(epoch)
    assert bars[0].spread == 1
    assert bars[0].real_volume == 0


def test_get_ohlcv_absent_spread_column_maps_to_none():
    state = _FakeState()
    state.ohlcv_npz = _ohlcv_npz([1_700_000_000], include_spread=False, include_real_volume=False)

    with _fake_gateway(state) as (base_url, _st):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        bars = client.get_ohlcv("WIN$", "M1", datetime(2023, 11, 14), datetime(2023, 11, 15))

    assert len(bars) == 1
    assert bars[0].spread is None
    assert bars[0].real_volume is None


def test_get_ticks_columnar_round_trip():
    time_msc = [1_700_000_000_000, 1_700_000_001_000, 1_700_000_002_000]
    state = _FakeState()
    state.ticks_npz = _ticks_npz(time_msc)

    with _fake_gateway(state) as (base_url, _st):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        result = client.get_ticks_columnar(
            "WIN$",
            datetime(2023, 11, 14, 22, 0),
            datetime(2023, 11, 14, 22, 5),
            use_cache=False,
        )

    assert set(result.keys()) == {
        "time_msc",
        "bid",
        "ask",
        "last",
        "volume",
        "flags",
    }
    assert result["time_msc"].dtype == np.int64
    assert result["bid"].dtype == np.float64
    assert result["flags"].dtype == np.int32
    assert list(result["time_msc"]) == time_msc


def test_get_ticks_columnar_empty_shape():
    with _fake_gateway() as (base_url, _st):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        result = client.get_ticks_columnar(
            "WIN$",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            use_cache=False,
        )

    assert len(result["time_msc"]) == 0
    assert result["time_msc"].dtype == np.int64
    assert result["flags"].dtype == np.int32


def test_health_caching_avoids_repeat_requests():
    with _fake_gateway() as (base_url, state):
        client = RemoteMt5Client(base_url=base_url)
        assert client.is_available() is True
        assert client.is_available() is True
        assert state.health_count == 1  # second call served from cache


def test_unreachable_gateway_fast_fails():
    client = RemoteMt5Client(base_url="http://127.0.0.1:9", health_timeout=0.5)
    assert client.is_available() is False


def test_schema_major_mismatch_is_unavailable(caplog):
    state = _FakeState()
    state.health = {
        "status": "ok",
        "schema_version": "2.0",
        "mt5_connected": True,
        "terminal_build": 1,
    }
    with _fake_gateway(state) as (base_url, _st):
        client = RemoteMt5Client(base_url=base_url)
        with caplog.at_level(logging.ERROR, logger="q_backend.market_data.clients.remote"):
            assert client.is_available() is False

    assert "incompatible" in caplog.text


def test_503_maps_to_connection_error():
    state = _FakeState()
    state.ohlcv_error = (503, {"error": "down", "code": "mt5_unavailable"})
    with _fake_gateway(state) as (base_url, _st):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        with pytest.raises(ConnectionError):
            client.get_ohlcv("WIN$", "M1", datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_400_maps_to_value_error():
    state = _FakeState()
    state.ohlcv_error = (400, {"error": "bad tf", "code": "invalid_timeframe"})
    with _fake_gateway(state) as (base_url, _st):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        with pytest.raises(ValueError):
            client.get_ohlcv("WIN$", "ZZ", datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_token_sent_when_configured():
    with _fake_gateway() as (base_url, state):
        client = RemoteMt5Client(base_url=base_url, token="s3cret")
        client.is_available()
    assert state.last_headers.get("X-Gateway-Token") == "s3cret"


def test_symbol_info_404_returns_none():
    state = _FakeState()
    state.symbol_info = None
    with _fake_gateway(state) as (base_url, _st):
        client = RemoteMt5Client(base_url=base_url)
        assert client.get_symbol_info("NOPE") is None


def test_available_range_round_trip():
    with _fake_gateway() as (base_url, _st):
        client = RemoteMt5Client(base_url=base_url)
        rng = client.get_available_ohlcv_range("WIN$", "D1")

    assert rng is not None
    assert rng.symbol == "WIN$"
    assert rng.timeframe == "D1"
    assert rng.bar_count == 1500


def test_flags_mapping_sent_to_gateway():
    with _fake_gateway() as (base_url, state):
        from datetime import datetime

        client = RemoteMt5Client(base_url=base_url)
        client.get_ticks_columnar(
            "WIN$",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            flags=COPY_TICKS_TRADE,
            use_cache=False,
        )
        client.get_ticks_columnar(
            "WIN$",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            flags=COPY_TICKS_ALL,
            use_cache=False,
        )

    assert any("flags=trade" in p for p in state.paths)
    assert any("flags=all" in p for p in state.paths)


def test_ticks_cache_namespaced_by_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_TICK_CACHE_DIR", str(tmp_path / "tickcache"))
    time_msc = [1_700_000_000_000, 1_700_000_001_000]
    state = _FakeState()
    state.ticks_npz = _ticks_npz(time_msc)

    from datetime import datetime

    with _fake_gateway(state) as (base_url, st):
        client = RemoteMt5Client(base_url=base_url)
        first = client.get_ticks_columnar(
            "WIN$",
            datetime(2023, 11, 14, 22, 0),
            datetime(2023, 11, 14, 22, 5),
            use_cache=True,
        )
        count_after_first = st.request_count
        # Change server payload; a cache hit must ignore it.
        st.ticks_npz = _ticks_npz([9_999_999_999_000])
        second = client.get_ticks_columnar(
            "WIN$",
            datetime(2023, 11, 14, 22, 0),
            datetime(2023, 11, 14, 22, 5),
            use_cache=True,
        )

    assert list(first["time_msc"]) == time_msc
    assert list(second["time_msc"]) == time_msc  # served from cache
    assert st.request_count == count_after_first  # no second HTTP call
