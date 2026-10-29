from __future__ import annotations

import io
import json
import threading

import numpy as np
import pytest

from tests.gateway.conftest import http_get, running_gateway_server
from tests.gateway.fake_metatrader5 import _TICK_DTYPE
from tests.gateway.test_mt5_gateway import _running_lane_server


def tick_source(fake_mt5, monkeypatch, times):
    ticks = np.array([(t // 1000, 99, 101, 100, 1, t, 32) for t in times], dtype=_TICK_DTYPE)
    calls = []

    def copy_ticks_from(symbol, start, count, flags):
        calls.append((symbol, start, count, flags))
        return ticks[ticks["time_msc"] >= start * 1000][:count]

    monkeypatch.setattr(fake_mt5, "copy_ticks_from", copy_ticks_from, raising=False)
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (1, "Success"))
    return calls


def decode(content):
    with np.load(io.BytesIO(content)) as archive:
        return archive["time_msc"], json.loads(str(archive["metadata"]))


def test_cursor_pages_preserve_duplicate_timestamps_and_skip_empty_prefix(gateway, fake_mt5, monkeypatch):
    calls = tick_source(fake_mt5, monkeypatch, [1500, 1500, 1500, 1501, 2200, 2200, 2900])
    app = gateway.GatewayApp()
    cursor, received = 1000, []
    while cursor is not None:
        times, meta = decode(app.ticks_page("WIN$", cursor, 2800, 2))
        received.extend(times.tolist())
        cursor = meta["next_cursor_msc"]
    assert received == [1500, 1500, 1500, 1501, 2200, 2200]
    assert meta["exhausted"] is True
    assert len(calls) < 10


def test_symbol_selection_is_reused_between_cursor_pages(gateway, fake_mt5, monkeypatch):
    tick_source(fake_mt5, monkeypatch, [1500, 1800, 2200, 2300])
    selected = []
    original = fake_mt5.symbol_select

    def select(symbol, enabled):
        selected.append(symbol)
        return original(symbol, enabled)

    monkeypatch.setattr(fake_mt5, "symbol_select", select)
    app = gateway.GatewayApp()
    _, meta = decode(app.ticks_page("WIN$", 1000, 2400, 2))
    decode(app.ticks_page("WIN$", meta["next_cursor_msc"], 2400, 2))
    assert selected == ["WIN$"]


def test_cursor_filters_replayed_second_prefix(gateway, fake_mt5, monkeypatch):
    tick_source(fake_mt5, monkeypatch, [1001, 1002, 1500, 1500, 1800])
    times, meta = decode(gateway.GatewayApp().ticks_page("WIN$", 1500, 2000, 2))
    assert times.tolist() == [1500, 1500, 1800]
    assert meta["next_cursor_msc"] is None


def test_native_failure_never_confirms_empty_history(gateway, fake_mt5, monkeypatch):
    monkeypatch.setattr(fake_mt5, "copy_ticks_from", lambda *args: np.empty(0, dtype=_TICK_DTYPE), raising=False)
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (-10005, "IPC timeout"))
    with pytest.raises(gateway.GatewayError, match="IPC timeout"):
        gateway.GatewayApp().ticks_page("WIN$", 1000, 2000, 2)


def test_dense_timestamp_group_fails_instead_of_losing_ticks(gateway, fake_mt5, monkeypatch):
    tick_source(fake_mt5, monkeypatch, [1500] * 9)
    monkeypatch.setattr(gateway, "_MAX_TICK_PAGE_ROWS", 8, raising=False)
    with pytest.raises(gateway.GatewayError, match="progress"):
        gateway.GatewayApp().ticks_page("WIN$", 1000, 2000, 2)


@pytest.mark.parametrize(
    "params",
    [
        {"start_msc": "2000", "end_msc": "1000"},
        {"start_msc": "0", "end_msc": "2000"},
        {"start_msc": "1000", "end_msc": "2000", "limit": "0"},
    ],
)
def test_page_endpoint_rejects_invalid_bounds(gateway, params):
    with running_gateway_server(gateway) as base:
        status, _, _ = http_get(base, "/v1/ticks/page", {"symbol": "WIN$", **params})
    assert status == 400


def test_page_endpoint_returns_arrays_and_cursor_metadata(gateway, fake_mt5, monkeypatch):
    tick_source(fake_mt5, monkeypatch, [1500, 2200])
    with running_gateway_server(gateway) as base:
        status, _, content = http_get(
            base, "/v1/ticks/page", {"symbol": "WIN$", "start_msc": "1000", "end_msc": "2000", "limit": "2"}
        )
    assert status == 200
    times, meta = decode(content)
    assert times.tolist() == [1500]
    assert meta == {"next_cursor_msc": None, "exhausted": True, "first_msc": 1500, "last_msc": 1500, "row_count": 1}


def test_blocked_tick_page_does_not_block_health_chart_or_trade_lanes(gateway, fake_mt5, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return np.empty(0, dtype=_TICK_DTYPE)

    monkeypatch.setattr(fake_mt5, "copy_ticks_from", blocked, raising=False)
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (1, "Success"))
    original_info = fake_mt5.terminal_info
    health_ipc_during_download = []

    def terminal_info():
        if entered.is_set():
            health_ipc_during_download.append(True)
        return original_info()

    monkeypatch.setattr(fake_mt5, "terminal_info", terminal_info)
    with _running_lane_server(gateway) as (base, _):
        http_get(base, "/v1/health")
        request = threading.Thread(
            target=http_get, args=(base, "/v1/ticks/page", {"symbol": "WIN$", "start_msc": "1000", "end_msc": "2000"})
        )
        request.start()
        assert entered.wait(2)
        try:
            assert http_get(base, "/v1/health")[0] == 200
            assert health_ipc_during_download == []
            assert http_get(base, "/v1/symbol_info", {"symbol": "WIN$"})[0] == 200
            assert (
                http_get(
                    base,
                    "/v1/trades",
                    {"symbol": "WIN$", "start_utc": "2026-10-01T12:00:00Z", "end_utc": "2026-10-01T12:01:00Z"},
                )[0]
                == 200
            )
        finally:
            release.set()
            request.join(5)


def test_fast_tick_archive_roundtrips_without_pickle(gateway):
    import zipfile

    arrays = {
        "time_msc": np.array([1001, 1001, 2000], dtype=np.int64),
        "last": np.array([10.0, np.nan, 12.0]),
        "flags": np.array([32, 64, 0], dtype=np.int32),
        "metadata": np.array(json.dumps({"exhausted": True})),
        "empty": np.array([], dtype=np.float64),
        "strided": np.arange(12)[::2],
    }
    content = gateway._savez_bytes(arrays, compression_level=1)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert all(item.compress_type == zipfile.ZIP_DEFLATED for item in archive.infolist())
    with np.load(io.BytesIO(content), allow_pickle=False) as archive:
        assert set(archive.files) == set(arrays)
        for key, expected in arrays.items():
            np.testing.assert_array_equal(archive[key], expected)
            assert archive[key].dtype == expected.dtype
