"""HTTP round-trip tests for the MT5 remote data gateway (WO183)."""

from __future__ import annotations

import io
import json
import multiprocessing
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

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


@pytest.mark.parametrize("partial", [False, True])
def test_ohlcv_scan_limit_never_returns_incomplete_success(gateway, fake_mt5, monkeypatch, partial):
    monkeypatch.setattr(gateway, "_MAX_HISTORY_CHUNKS", 1)
    if partial:
        fake_mt5._state["rates_queue"] = [
            np.array([(_epoch(datetime(2026, 1, 1)), 100, 101, 99, 100, 10, 0, 0)], dtype=_RATE_DTYPE)
        ]
    with pytest.raises(gateway.GatewayError, match="history scan.*narrower range") as error:
        gateway._fetch_ohlcv_chunked("WIN$N", fake_mt5.TIMEFRAME_M1, datetime(2026, 1, 1), datetime(2026, 2, 1))
    assert error.value.status == 500
    assert error.value.code == "internal_error"


def test_ohlcv_final_scan_chunk_can_complete(gateway, fake_mt5, monkeypatch):
    monkeypatch.setattr(gateway, "_MAX_HISTORY_CHUNKS", 1)
    rates, truncated = gateway._fetch_ohlcv_chunked(
        "WIN$N", fake_mt5.TIMEFRAME_M1, datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert len(rates) == 0
    assert truncated is False


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
        assert "metadata" in npz.files
        meta = json.loads(str(npz["metadata"]))
        assert meta["truncated"] is False
        assert meta["max_bars"] == 50_000


def test_ohlcv_truncation_reporting_inside_chunk_and_on_boundary(gateway, fake_mt5, monkeypatch):
    # Patch limit to a small number, e.g., 5 bars, on the loaded gateway module
    monkeypatch.setattr(gateway, "_MAX_OHLCV_BARS", 5)

    base_epoch = _epoch(datetime(2026, 1, 5, 9, 0, 0))

    # Case 1: cut inside a chunk (chunk has 7 bars, limit is 5)
    rates_inside = np.array(
        [(base_epoch + i * 60, 100.0, 101.0, 99.0, 100.5, 10, 1, 5) for i in range(7)],
        dtype=_RATE_DTYPE,
    )
    fake_mt5._state["rates_queue"] = [rates_inside]

    with running_gateway_server(gateway) as base:
        status, headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "WIN$",
                "timeframe": "M1",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:10:00",
            },
        )

    assert status == 200
    with np.load(io.BytesIO(body)) as npz:
        assert len(npz["time"]) == 5
        assert "metadata" in npz.files
        meta = json.loads(str(npz["metadata"]))
        assert meta["truncated"] is True
        assert meta["max_bars"] == 5

    # Case 2: cut exactly on a chunk boundary
    # Chunk 1 has 3 bars, chunk 2 has 2 bars (total reaches exactly 5), chunk 3 has 2 bars
    rates_chunk1 = np.array(
        [(base_epoch + i * 60, 100.0, 101.0, 99.0, 100.5, 10, 1, 5) for i in range(3)],
        dtype=_RATE_DTYPE,
    )
    rates_chunk2 = np.array(
        [(base_epoch + (3 + i) * 60, 100.0, 101.0, 99.0, 100.5, 10, 1, 5) for i in range(2)],
        dtype=_RATE_DTYPE,
    )
    rates_chunk3 = np.array(
        [(base_epoch + (5 + i) * 60, 100.0, 101.0, 99.0, 100.5, 10, 1, 5) for i in range(2)],
        dtype=_RATE_DTYPE,
    )
    # The range end is after all chunks so the loop doesn't end before trying chunk 3
    fake_mt5._state["rates_queue"] = [rates_chunk1, rates_chunk2, rates_chunk3]

    with running_gateway_server(gateway) as base:
        status, headers, body = http_get(
            base,
            "/v1/ohlcv",
            {
                "symbol": "WIN$",
                "timeframe": "M1",
                "start": "2026-01-05T09:00:00",
                "end": "2026-01-05T09:10:00",
            },
        )

    assert status == 200
    with np.load(io.BytesIO(body)) as npz:
        assert len(npz["time"]) == 5
        assert "metadata" in npz.files
        meta = json.loads(str(npz["metadata"]))
        assert meta["truncated"] is True
        assert meta["max_bars"] == 5


def test_recent_ohlcv_uses_one_bounded_positional_read(gateway, fake_mt5):
    base_epoch = _epoch(datetime(2026, 1, 5, 9, 0, 0))
    rates = np.array(
        [(base_epoch + i * 3600, 130000.0, 130100.0, 129900.0, 130050.0, 500, 1, 250) for i in range(3)],
        dtype=_RATE_DTYPE,
    )
    fake_mt5._state["rates_from_pos"] = rates
    fake_mt5._state["known_symbols"].add("CCM$")

    with running_gateway_server(gateway) as base:
        status, headers, body = http_get(
            base,
            "/v1/ohlcv/recent",
            {"symbol": "CCM$", "timeframe": "H1", "count": "2"},
        )

    assert status == 200
    assert headers["Content-Type"] == "application/octet-stream"
    assert fake_mt5._state["last_rates_from_pos"] == ("CCM$", fake_mt5.TIMEFRAME_H1, 0, 2)
    with np.load(io.BytesIO(body)) as npz:
        assert list(npz["time"]) == [base_epoch + 3600, base_epoch + 7200]
        assert "metadata" not in npz.files


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


# -- /v1/trades -------------------------------------------------------------------------

_TRADE_DTYPE = _TICK_DTYPE + [("volume_real", "f8")]
_QUOTE = 2 | 4  # TICK_FLAG_BID | TICK_FLAG_ASK
_LAST = 8
_VOLUME = 16
_BUY = 32
_SELL = 64
_T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
_T0_MSC = int(_T0.timestamp() * 1000)


def _trade_ticks(rows, dtype=_TRADE_DTYPE):
    """rows: (offset_ms, last, volume, volume_real, flags); volume_real is dropped for the legacy dtype."""
    records = []
    for offset, last, volume, volume_real, flags in rows:
        msc = _T0_MSC + offset
        record = (msc // 1000, 1.0, 2.0, last, volume, msc, flags)
        records.append(record + (volume_real,) if dtype is _TRADE_DTYPE else record)
    return np.array(records, dtype=dtype)


def _get_trades(base, start=_T0, end=None, **extra):
    end = end or _T0.replace(minute=5)
    return http_get(
        base,
        "/v1/trades",
        {"symbol": "WIN$", "start_utc": start.isoformat(), "end_utc": end.isoformat(), **extra},
    )


def _load(body):
    with np.load(io.BytesIO(body)) as npz:
        arrays = {name: npz[name] for name in npz.files if name != "metadata"}
        return arrays, json.loads(str(npz["metadata"]))


def test_trades_keep_same_millisecond_records_and_assign_occurrence(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks(
        [
            (0, 100.0, 2.0, 2.0, _LAST | _VOLUME),
            (0, 100.0, 2.0, 2.0, _LAST | _VOLUME),
            (0, 101.0, 1.0, 1.0, _BUY | _LAST),
            (7, 100.5, 3.0, 3.0, _SELL | _LAST),
        ]
    )
    with running_gateway_server(gateway) as base:
        status, _headers, body = _get_trades(base)

    assert status == 200
    arrays, meta = _load(body)
    assert list(arrays["time_msc"]) == [_T0_MSC] * 3 + [_T0_MSC + 7]
    assert list(arrays["occurrence"]) == [0, 1, 2, 0]
    assert list(arrays["price"]) == [100.0, 100.0, 101.0, 100.5]
    assert list(arrays["raw_flags"]) == [_LAST | _VOLUME, _LAST | _VOLUME, _BUY | _LAST, _SELL | _LAST]
    assert fake_mt5._state["last_flags"] == fake_mt5.COPY_TICKS_ALL
    assert meta["range_complete"] is True
    assert meta["availability"] == "available"
    assert meta["invalid_trade_count"] == 0
    assert meta["truncated"] is False
    assert meta["covered_from_utc"] == "2026-10-01T12:00:00+00:00"
    assert meta["covered_to_utc"] == "2026-10-01T12:05:00+00:00"


def test_trades_exclude_quote_only_updates_even_with_carried_last_and_volume(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks(
        [
            (0, 100.0, 2.0, 2.0, _QUOTE),
            (1, 100.0, 2.0, 2.0, _LAST | _VOLUME),
            (2, 100.0, 2.0, 2.0, _QUOTE),
        ]
    )
    with running_gateway_server(gateway) as base:
        _status, _headers, body = _get_trades(base)

    arrays, meta = _load(body)
    assert list(arrays["time_msc"]) == [_T0_MSC + 1]
    assert meta["invalid_trade_count"] == 0
    assert meta["range_complete"] is True


def test_trades_treat_both_or_neither_aggressor_flags_as_eligible(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks(
        [(0, 100.0, 1.0, 1.0, _BUY | _SELL | _LAST), (1, 100.0, 1.0, 1.0, _LAST)]
    )
    with running_gateway_server(gateway) as base:
        _status, _headers, body = _get_trades(base)

    arrays, _meta = _load(body)
    assert list(arrays["raw_flags"]) == [_BUY | _SELL | _LAST, _LAST]


def test_trades_prefer_positive_volume_real_and_report_field_and_unit(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks([(0, 100.0, 5.0, 3.0, _LAST), (1, 100.0, 7.0, 4.0, _LAST)])
    with running_gateway_server(gateway) as base:
        _status, _headers, body = _get_trades(base)

    arrays, meta = _load(body)
    assert meta["volume_field"] == "volume_real"
    assert meta["volume_unit"] == "contracts"
    # Both raw fields travel unchanged; the metadata selects the analysis field.
    assert list(arrays["volume"]) == [5.0, 7.0]
    assert list(arrays["volume_real"]) == [3.0, 4.0]


def test_trades_fall_back_to_raw_volume_when_volume_real_is_absent_or_unusable(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        fake_mt5._state["ticks_source"] = _trade_ticks([(0, 100.0, 5.0, 0.0, _LAST)], dtype=_TICK_DTYPE)
        _status, _headers, body = _get_trades(base)
        absent, absent_meta = _load(body)

        fake_mt5._state["ticks_source"] = _trade_ticks([(0, 100.0, 5.0, 0.0, _LAST)])
        _status, _headers, body = _get_trades(base)
        zero, zero_meta = _load(body)

    assert "volume_real" not in absent
    assert absent_meta["volume_field"] == "volume"
    assert absent_meta["volume_unit"] == "provider-lots"
    assert zero_meta["volume_field"] == "volume"
    assert list(zero["volume"]) == [5.0]


def test_trades_count_invalid_trade_records_and_mark_range_incomplete(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks(
        [
            (0, 100.0, 2.0, 2.0, _LAST),
            (1, 0.0, 2.0, 2.0, _LAST),
            (2, float("nan"), 2.0, 2.0, _LAST),
            (3, 100.0, 0.0, 0.0, _VOLUME),
        ]
    )
    with running_gateway_server(gateway) as base:
        _status, _headers, body = _get_trades(base)

    arrays, meta = _load(body)
    assert list(arrays["time_msc"]) == [_T0_MSC]
    assert meta["invalid_trade_count"] == 3
    assert meta["range_complete"] is False
    assert meta["coverage_reason"] == "invalid_trade_records"


def test_trades_range_is_half_open_in_utc(gateway, fake_mt5):
    fake_mt5._state["ticks_source"] = _trade_ticks(
        [
            (-1, 99.0, 1.0, 1.0, _LAST),
            (0, 100.0, 1.0, 1.0, _LAST),
            (299_999, 101.0, 1.0, 1.0, _LAST),
            (300_000, 102.0, 1.0, 1.0, _LAST),
        ]
    )
    with running_gateway_server(gateway) as base:
        # The same instant expressed with a non-UTC offset must select the same rows.
        offset = timezone(timedelta(hours=-3))
        _status, _headers, body = _get_trades(base, start=_T0.astimezone(offset))

    arrays, _meta = _load(body)
    assert list(arrays["price"]) == [100.0, 101.0]
    start, end, _flags = fake_mt5._state["tick_range_calls"][0]
    assert start.utcoffset().total_seconds() == 0 and end.utcoffset().total_seconds() == 0


def test_trades_report_truncation_instead_of_a_partial_page(gateway, fake_mt5, monkeypatch):
    monkeypatch.setattr(gateway, "_MAX_TRADE_ROWS", 2)
    fake_mt5._state["ticks_source"] = _trade_ticks([(i, 100.0, 1.0, 1.0, _LAST) for i in range(3)])
    with running_gateway_server(gateway) as base:
        _status, _headers, body = _get_trades(base)

    arrays, meta = _load(body)
    assert len(arrays["time_msc"]) == 0
    assert meta["truncated"] is True
    assert meta["range_complete"] is False
    assert meta["coverage_reason"] == "range_truncated"


def test_trades_report_source_outage_when_the_terminal_returns_none(gateway, fake_mt5):
    fake_mt5._state["ticks_queue"] = [None]
    with running_gateway_server(gateway) as base:
        status, _headers, body = _get_trades(base)

    assert status == 200
    _arrays, meta = _load(body)
    assert meta["availability"] == "unavailable"
    assert meta["range_complete"] is False
    assert meta["coverage_reason"] == "source_error"
    assert meta["covered_from_utc"] is None


def test_trades_reject_naive_or_inverted_ranges(gateway, fake_mt5):
    with running_gateway_server(gateway) as base:
        naive, _h, naive_body = http_get(
            base,
            "/v1/trades",
            {"symbol": "WIN$", "start_utc": "2026-10-01T12:00:00", "end_utc": "2026-10-01T12:05:00Z"},
        )
        inverted, _h, _b = _get_trades(base, start=_T0.replace(minute=5), end=_T0)

    assert naive == 400
    assert json.loads(naive_body)["code"] == "invalid_datetime"
    assert inverted == 400


def test_trades_leave_ticks_endpoint_behavior_unchanged(gateway, fake_mt5):
    fake_mt5._state["ticks_queue"] = [_trade_ticks([(0, 100.0, 1.0, 1.0, _QUOTE)], dtype=_TICK_DTYPE)]
    with running_gateway_server(gateway) as base:
        status, _headers, body = http_get(
            base, "/v1/ticks", {"symbol": "WIN$", "start": "2026-10-01T12:00:00", "end": "2026-10-01T12:05:00"}
        )

    assert status == 200
    with np.load(io.BytesIO(body)) as npz:
        assert sorted(npz.files) == sorted(["time_msc", "bid", "ask", "last", "volume", "flags"])
        assert len(npz["time_msc"]) == 1


# ---------------------------------------------------------------------------
# Worker lanes: the HTTP process forwards every MT5 call to a per-lane worker.
# ---------------------------------------------------------------------------
class _ThreadProcess:
    """Stands in for a spawned worker process; the worker loop runs on a thread."""

    def __init__(self, gateway, child):
        self._child = child
        self._thread = threading.Thread(target=gateway._worker_main, args=(child,), daemon=True)
        self._thread.start()
        self.terminated = False

    def is_alive(self) -> bool:
        return not self.terminated and self._thread.is_alive()

    def terminate(self) -> None:
        self.terminated = True

    def join(self, timeout=None) -> None:
        pass


@contextmanager
def _running_lane_server(gateway, timeout_s: float = 5.0):
    spawned: list[_ThreadProcess] = []

    def spawn():
        connection, child = multiprocessing.Pipe()
        spawned.append(_ThreadProcess(gateway, child))
        return spawned[-1], connection

    server = gateway.build_server("127.0.0.1", 0)
    server.app.lanes = {
        lane: gateway.Mt5Worker(lane, wait_s, timeout_s=timeout_s, spawn=spawn)
        for lane, wait_s in {"chart": 0.2, "ticks": 0.2, "trades": 0.0}.items()
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", spawned
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for worker in server.app.lanes.values():
            worker.close()


def _block_recent_bars(fake_mt5, monkeypatch) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()

    def blocked(*_args):
        entered.set()
        release.wait(10)
        return None

    monkeypatch.setattr(fake_mt5, "copy_rates_from_pos", blocked)
    return entered, release


_RECENT = {"symbol": "WIN$", "timeframe": "M5", "count": "10"}
_TICKS = {"symbol": "WIN$", "start": "2026-01-05T09:00:00", "end": "2026-01-05T09:00:05"}


def test_lanes_serve_requests_through_worker_processes(gateway, fake_mt5):
    with _running_lane_server(gateway) as (base, _spawned):
        health_status, _h, health_body = http_get(base, "/v1/health")
        info_status, _h2, info_body = http_get(base, "/v1/symbol_info", {"symbol": "WIN$"})
        missing_status, _h3, missing_body = http_get(base, "/v1/symbol_info", {"symbol": "NOPE"})

    assert health_status == 200
    assert json.loads(health_body)["mt5_connected"] is True
    assert json.loads(health_body)["terminal_build"] == 4200
    assert info_status == 200
    assert json.loads(info_body)["name"] == "WIN$"
    assert missing_status == 404
    assert json.loads(missing_body)["code"] == "symbol_not_found"


def test_blocked_chart_call_leaves_health_and_other_lanes_responsive(gateway, fake_mt5, monkeypatch):
    entered, release = _block_recent_bars(fake_mt5, monkeypatch)
    with _running_lane_server(gateway) as (base, _spawned):
        http_get(base, "/v1/health")
        blocked = threading.Thread(target=http_get, args=(base, "/v1/ohlcv/recent", _RECENT), daemon=True)
        blocked.start()
        assert entered.wait(5)
        try:
            health_status, _h, health_body = http_get(base, "/v1/health")
            ticks_status, _h2, _b2 = http_get(base, "/v1/ticks", _TICKS)
            busy_status, _h3, busy_body = http_get(base, "/v1/symbol_info", {"symbol": "WIN$"})
        finally:
            release.set()
            blocked.join(timeout=5)

    assert health_status == 200
    assert json.loads(health_body)["mt5_connected"] is True
    assert ticks_status == 200
    assert busy_status == 503
    assert json.loads(busy_body)["code"] == "chart_busy"


def test_native_call_timeout_restarts_the_lane_worker(gateway, fake_mt5, monkeypatch):
    _entered, release = _block_recent_bars(fake_mt5, monkeypatch)
    with _running_lane_server(gateway, timeout_s=0.2) as (base, spawned):
        try:
            timeout_status, _h, timeout_body = http_get(base, "/v1/ohlcv/recent", _RECENT)
            retry_status, _h2, _b2 = http_get(base, "/v1/symbol_info", {"symbol": "WIN$"})
        finally:
            release.set()

    assert timeout_status == 503
    assert json.loads(timeout_body)["code"] == "chart_timeout"
    assert retry_status == 200
    assert spawned[0].terminated is True
    assert len(spawned) == 4
