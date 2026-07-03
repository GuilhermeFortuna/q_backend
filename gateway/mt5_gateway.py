"""MT5 remote market-data gateway (WO183).

A single-file, dependency-minimal HTTP server that runs on Windows-Python inside a
Wine prefix (or any Windows box) next to a MetaTrader 5 terminal and exposes MT5
*read-only* market data. The Linux backend consumes it through a protocol-compatible
client (WO184). Live execution stays native-MT5-on-Windows only — this module never
imports or exposes any trading/order API.

Runtime contract
================
Dependencies: **stdlib ``http.server`` + ``MetaTrader5`` + ``numpy`` only**. It lives
deliberately outside ``src/`` so it can never import ``q_backend`` — the Wine Python has
only stdlib, ``MetaTrader5`` and ``numpy`` (numpy is already a hard dependency of the
MetaTrader5 package, so nothing extra is installed).

Schema version: ``SCHEMA_VERSION = "1.0"``. All endpoints are under ``/v1/``. Breaking
wire changes bump ``SCHEMA_VERSION`` major and move endpoints to ``/v2/`` — the ``/v1/``
semantics are never mutated.

Endpoints
=========
========================================================  ======  ==========================================================
Endpoint                                                  Method  Response
========================================================  ======  ==========================================================
``/v1/health``                                            GET     JSON ``{status, schema_version, mt5_connected,
                                                                  terminal_build}`` — always HTTP 200
``/v1/symbol_info?symbol=``                               GET     JSON dict | 404
``/v1/symbols/search?query=``                             GET     JSON list
``/v1/available_range?symbol=&timeframe=``                GET     JSON ``{symbol, timeframe, start, end, bar_count}`` | 404
``/v1/ohlcv?symbol=&timeframe=&start=&end=``              GET     ``.npz`` (application/octet-stream)
``/v1/ticks?symbol=&start=&end=&flags=``                  GET     ``.npz`` (application/octet-stream)
========================================================  ======  ==========================================================

Wire contract
=============
- ``start`` / ``end`` query params are **ISO-8601 naive Brasília wall-clock** strings —
  the same naive datetimes ``MetaTraderClient`` passes to ``mt5.copy_rates_range``. The
  gateway parses them with ``datetime.fromisoformat`` and passes them straight through.
  There is **no timezone conversion anywhere in the gateway**: raw MT5 epochs go out,
  naive-Brasília ISO comes in.
- ``/v1/ohlcv`` ``.npz`` arrays, all equal length: ``time`` (int64, raw MT5 epoch
  seconds exactly as returned by ``copy_rates_range``), ``open`` / ``high`` / ``low`` /
  ``close`` (float64), ``tick_volume`` (int64), and ``spread`` / ``real_volume`` (int64)
  only when present in the rates dtype.
- ``/v1/ticks`` ``.npz`` arrays with the columnar tick keys: ``time_msc`` int64 (raw MT5
  milliseconds), ``bid`` / ``ask`` / ``last`` / ``volume`` float64, ``flags`` int32. The
  ``flags`` query param is ``all`` (default) or ``trade``, mapped to
  ``mt5.COPY_TICKS_ALL`` / ``mt5.COPY_TICKS_TRADE``.
- Bulk serialization is ``np.savez_compressed`` into an in-memory buffer, served as
  ``application/octet-stream``. JSON is only used for ``/v1/health``,
  ``/v1/symbol_info``, ``/v1/symbols/search``, ``/v1/available_range`` and for errors
  (``{"error": str, "code": str}`` with a proper HTTP status).

Errors
======
- Unknown timeframe               -> 400 ``invalid_timeframe``
- Symbol not selectable / unknown -> 404 ``symbol_not_found``
- MT5 not initialized             -> 503 ``mt5_unavailable`` (``/v1/health`` still 200,
                                     with ``mt5_connected: false``)
- Missing token (when configured) -> 401 ``unauthorized``

Launch command (WO186 installs this via a systemd user unit)
============================================================
Run under the Wine prefix's Windows Python::

    wine python mt5_gateway.py --host 127.0.0.1 --port 18812

Optionally protect a non-localhost deployment with a shared secret; when ``--token`` is
set every request must carry the header ``X-Gateway-Token: <token>``::

    wine python mt5_gateway.py --host 0.0.0.0 --port 18812 --token "$MT5_GATEWAY_TOKEN"

Config can also come from the environment: ``MT5_GATEWAY_HOST``, ``MT5_GATEWAY_PORT``,
``MT5_GATEWAY_TOKEN``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

import MetaTrader5 as mt5

SCHEMA_VERSION = "1.0"

logger = logging.getLogger("mt5_gateway")

# ---------------------------------------------------------------------------
# Fetch tuning — copied from MetaTraderClient so gateway bars/ticks are
# byte-identical to natively-fetched ones (no q_backend import allowed).
# ---------------------------------------------------------------------------
_MAX_HISTORY_CHUNKS = 1_000
_MAX_OHLCV_BARS = 50_000
_MAX_TICKS = 50_000_000
_TICK_RANGE_FETCH_DAYS = 7
_HISTORY_ANCHOR = datetime(1990, 1, 1)

# Mirrors MetaTraderClient.TIMEFRAME_NAMES.
TIMEFRAME_NAMES = (
    "M1",
    "M2",
    "M3",
    "M4",
    "M5",
    "M6",
    "M10",
    "M12",
    "M15",
    "M20",
    "M30",
    "H1",
    "H2",
    "H3",
    "H4",
    "H6",
    "H8",
    "H12",
    "D1",
    "W1",
    "MN1",
)

COLUMNAR_TICK_KEYS = ("time_msc", "bid", "ask", "last", "volume", "flags")

_OHLCV_BASE_DTYPE = np.dtype(
    [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "i8"),
    ]
)


class GatewayError(Exception):
    """HTTP-mappable error carrying a status code and a stable machine code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Timestamp helpers — NO timezone math. ``_epoch_to_naive`` is byte-identical to
# ``q_backend.market_data.timezone.unix_seconds_to_brasilia_naive``: it takes the raw
# MT5 epoch and reads its UTC wall clock as a naive datetime (no BRT/UTC offset). This
# is only used internally to advance the chunk cursor for the next range request,
# exactly like MetaTraderClient does. The bytes we serve are the raw epochs, untouched.
# ---------------------------------------------------------------------------
def _epoch_to_naive(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).replace(tzinfo=None)


def _msc_to_naive(msc: int) -> datetime:
    sec = msc // 1000
    ms_remainder = msc % 1000
    return _epoch_to_naive(sec) + timedelta(milliseconds=ms_remainder)


def _empty_ticks_columnar() -> dict[str, np.ndarray]:
    return {
        "time_msc": np.array([], dtype=np.int64),
        "bid": np.array([], dtype=np.float64),
        "ask": np.array([], dtype=np.float64),
        "last": np.array([], dtype=np.float64),
        "volume": np.array([], dtype=np.float64),
        "flags": np.array([], dtype=np.int32),
    }


def _ticks_structured_to_columnar(ticks: np.ndarray) -> dict[str, np.ndarray]:
    """Mirror of MetaTraderClient._ticks_structured_to_columnar (raw values, no tz)."""
    if ticks is None or len(ticks) == 0:
        return _empty_ticks_columnar()

    names = ticks.dtype.names or ()
    has_time_msc = "time_msc" in names
    has_last = "last" in names
    has_volume = "volume" in names
    has_flags = "flags" in names
    count = len(ticks)

    if has_time_msc:
        time_msc = ticks["time_msc"].astype(np.int64, copy=False)
    else:
        time_msc = ticks["time"].astype(np.int64, copy=False) * 1000

    bid = ticks["bid"].astype(np.float64, copy=False)
    ask = ticks["ask"].astype(np.float64, copy=False)
    last = (
        ticks["last"].astype(np.float64, copy=False)
        if has_last
        else np.zeros(count, dtype=np.float64)
    )
    volume = (
        ticks["volume"].astype(np.float64, copy=False)
        if has_volume
        else np.zeros(count, dtype=np.float64)
    )
    flags = (
        ticks["flags"].astype(np.int32, copy=False)
        if has_flags
        else np.zeros(count, dtype=np.int32)
    )

    return {
        "time_msc": time_msc,
        "bid": bid,
        "ask": ask,
        "last": last,
        "volume": volume,
        "flags": flags,
    }


def _resolve_timeframe(timeframe: str) -> int:
    """Validate against TIMEFRAME_NAMES and resolve the mt5.TIMEFRAME_* constant."""
    name = timeframe.upper()
    if name not in TIMEFRAME_NAMES:
        raise GatewayError(
            400,
            "invalid_timeframe",
            f"Unknown timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_NAMES)}",
        )
    value = getattr(mt5, f"TIMEFRAME_{name}", None)
    if value is None:
        raise GatewayError(
            400,
            "invalid_timeframe",
            f"MT5 has no timeframe constant TIMEFRAME_{name}.",
        )
    return int(value)


def _resolve_tick_flags(value: str | None) -> int:
    if value is None or value.lower() == "all":
        return int(mt5.COPY_TICKS_ALL)
    if value.lower() == "trade":
        return int(mt5.COPY_TICKS_TRADE)
    raise GatewayError(
        400,
        "invalid_flags",
        f"Invalid flags '{value}'. Expected 'all' or 'trade'.",
    )


def _get_chunk_days(mt5_timeframe: int) -> int:
    """Mirror of MetaTraderClient._get_chunk_days (single-request bar-limit guard)."""
    if mt5_timeframe == mt5.TIMEFRAME_M1:
        return 15
    if mt5_timeframe in (
        mt5.TIMEFRAME_M2,
        mt5.TIMEFRAME_M3,
        mt5.TIMEFRAME_M4,
        mt5.TIMEFRAME_M5,
        mt5.TIMEFRAME_M6,
    ):
        return 60
    if mt5_timeframe in (
        mt5.TIMEFRAME_M10,
        mt5.TIMEFRAME_M12,
        mt5.TIMEFRAME_M15,
        mt5.TIMEFRAME_M20,
        mt5.TIMEFRAME_M30,
    ):
        return 180
    # H1, H4, D1, etc.
    return 365


def _fetch_ohlcv_chunked(
    symbol: str, mt5_timeframe: int, start: datetime, end: datetime
) -> np.ndarray:
    """Reimplementation of MetaTraderClient._fetch_ohlcv_range_chunked (gateway-side).

    Must be called with the MT5 lock held. Returns a concatenated structured rates
    array (possibly zero-length) with raw MT5 columns preserved.
    """
    chunks: list[np.ndarray] = []
    total_bars = 0
    cursor = start
    chunk_days = _get_chunk_days(mt5_timeframe)

    for _ in range(_MAX_HISTORY_CHUNKS):
        if cursor > end:
            break

        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)

        if rates is not None and len(rates) > 0:
            chunk_len = len(rates)
            if total_bars + chunk_len > _MAX_OHLCV_BARS:
                remaining = _MAX_OHLCV_BARS - total_bars
                if remaining <= 0:
                    break
                rates = rates[:remaining]
                chunk_len = remaining

            chunks.append(rates)
            total_bars += chunk_len
            next_cursor = _epoch_to_naive(int(rates[-1]["time"])) + timedelta(seconds=1)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        else:
            cursor = chunk_end + timedelta(seconds=1)

        if total_bars >= _MAX_OHLCV_BARS:
            break

    if not chunks:
        return np.empty(0, dtype=_OHLCV_BASE_DTYPE)
    return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


def _fetch_ticks_chunked(
    symbol: str, start: datetime, end: datetime, flags: int
) -> dict[str, np.ndarray]:
    """Reimplementation of MetaTraderClient._fetch_ticks_range_chunked (gateway-side).

    Must be called with the MT5 lock held. Returns columnar tick arrays with raw
    ``time_msc`` values preserved.
    """
    chunks: list[np.ndarray] = []
    total_ticks = 0
    cursor = start

    for _ in range(_MAX_HISTORY_CHUNKS):
        if cursor > end:
            break

        chunk_end = min(cursor + timedelta(days=_TICK_RANGE_FETCH_DAYS), end)
        ticks = mt5.copy_ticks_range(symbol, cursor, chunk_end, flags)

        if ticks is not None and len(ticks) > 0:
            chunk_len = len(ticks)
            if total_ticks + chunk_len > _MAX_TICKS:
                raise GatewayError(
                    400,
                    "tick_range_too_large",
                    f"Tick range for {symbol} exceeds the maximum of "
                    f"{_MAX_TICKS:,} ticks; narrow the start/end window.",
                )

            chunks.append(ticks)
            total_ticks += chunk_len

            names = ticks.dtype.names or ()
            if "time_msc" in names:
                last_msc = int(ticks["time_msc"][-1])
                next_cursor = _msc_to_naive(last_msc) + timedelta(milliseconds=1)
            else:
                last_sec = int(ticks["time"][-1])
                next_cursor = _epoch_to_naive(last_sec) + timedelta(seconds=1)

            if next_cursor <= cursor:
                break
            cursor = next_cursor
        else:
            cursor = chunk_end + timedelta(seconds=1)

        if total_ticks >= _MAX_TICKS:
            break

    if not chunks:
        return _empty_ticks_columnar()

    all_ticks = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    return _ticks_structured_to_columnar(all_ticks)


def _ohlcv_to_npz_bytes(rates: np.ndarray) -> bytes:
    names = rates.dtype.names or ()
    arrays: dict[str, np.ndarray] = {
        "time": np.asarray(rates["time"], dtype=np.int64),
        "open": np.asarray(rates["open"], dtype=np.float64),
        "high": np.asarray(rates["high"], dtype=np.float64),
        "low": np.asarray(rates["low"], dtype=np.float64),
        "close": np.asarray(rates["close"], dtype=np.float64),
        "tick_volume": np.asarray(rates["tick_volume"], dtype=np.int64),
    }
    if "spread" in names:
        arrays["spread"] = np.asarray(rates["spread"], dtype=np.int64)
    if "real_volume" in names:
        arrays["real_volume"] = np.asarray(rates["real_volume"], dtype=np.int64)
    return _savez_bytes(arrays)


def _savez_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def _json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _to_plain_dict(obj) -> dict:
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    if isinstance(obj, dict):
        return dict(obj)
    return dict(vars(obj))


class GatewayApp:
    """Holds MT5 connection state and serializes every ``mt5.*`` call.

    The MT5 IPC is not thread-safe, so all terminal access happens under a single
    lock. ``mt5.initialize()`` is best-effort at startup and retried lazily per
    request if it previously failed.
    """

    def __init__(self, token: str | None = None):
        # A blank token (e.g. `MT5_GATEWAY_TOKEN=` left empty in the env file) means
        # "no auth required", not "require an empty header".
        self.token = (token or "").strip() or None
        self._lock = threading.Lock()
        self._initialized = False

    # -- connection management (call under lock, except try_initialize) -----
    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return True
        if mt5.initialize():
            self._initialized = True
            logger.info("MetaTrader5 initialized.")
            return True
        code, desc = _safe_last_error()
        logger.warning("mt5.initialize() failed: %s (code %s)", desc, code)
        return False

    def _require_ready(self) -> None:
        if not self._ensure_initialized():
            raise GatewayError(
                503,
                "mt5_unavailable",
                "MetaTrader 5 terminal is not initialized.",
            )

    def try_initialize(self) -> bool:
        with self._lock:
            return self._ensure_initialized()

    def shutdown_mt5(self) -> None:
        with self._lock:
            if self._initialized:
                mt5.shutdown()
                self._initialized = False
                logger.info("MetaTrader5 shut down.")

    # -- endpoints ----------------------------------------------------------
    def health(self) -> dict:
        with self._lock:
            connected = self._ensure_initialized()
            build = _terminal_build() if connected else None
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "mt5_connected": connected,
            "terminal_build": build,
        }

    def symbol_info(self, symbol: str) -> dict:
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise GatewayError(
                    404, "symbol_not_found", f"Symbol '{symbol}' is not selectable."
                )
            info = mt5.symbol_info(symbol)
            if info is None:
                raise GatewayError(
                    404, "symbol_not_found", f"No symbol_info for '{symbol}'."
                )
            return _to_plain_dict(info)

    def search_symbols(self, query: str) -> list[dict]:
        with self._lock:
            self._require_ready()
            pattern = f"*{query.upper()}*"
            symbols = mt5.symbols_get(pattern)
            if symbols is None:
                return []
            return [
                {
                    "name": s.name,
                    "description": s.description,
                    "path": s.path,
                    "custom": s.custom,
                }
                for s in symbols
            ]

    def available_range(self, symbol: str, timeframe: str) -> dict:
        mt5_timeframe = _resolve_timeframe(timeframe)
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise GatewayError(
                    404, "symbol_not_found", f"Symbol '{symbol}' is not selectable."
                )

            earliest = _probe_earliest_bar(symbol, mt5_timeframe)
            latest = _probe_latest_bar(symbol, mt5_timeframe)
            if earliest is None or latest is None:
                raise GatewayError(
                    404,
                    "range_unavailable",
                    f"No OHLCV history available for '{symbol}' {timeframe}.",
                )
            bar_count = _count_bars_between(symbol, mt5_timeframe, earliest, latest)

        return {
            "symbol": symbol,
            "timeframe": timeframe.upper(),
            "start": earliest.isoformat(),
            "end": latest.isoformat(),
            "bar_count": bar_count,
        }

    def ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> bytes:
        mt5_timeframe = _resolve_timeframe(timeframe)
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise GatewayError(
                    404, "symbol_not_found", f"Symbol '{symbol}' is not selectable."
                )
            rates = _fetch_ohlcv_chunked(symbol, mt5_timeframe, start, end)
        return _ohlcv_to_npz_bytes(rates)

    def ticks(
        self, symbol: str, start: datetime, end: datetime, flags: int
    ) -> bytes:
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise GatewayError(
                    404, "symbol_not_found", f"Symbol '{symbol}' is not selectable."
                )
            columnar = _fetch_ticks_chunked(symbol, start, end, flags)
        return _savez_bytes(columnar)


def _safe_last_error() -> tuple[int, str]:
    last_error = getattr(mt5, "last_error", None)
    if last_error is None:
        return (0, "unknown")
    try:
        code, desc = last_error()
        return int(code), str(desc)
    except Exception:  # noqa: BLE001 - diagnostic only; never fail because of it
        return (0, "unknown")


def _terminal_build() -> int | None:
    info_fn = getattr(mt5, "terminal_info", None)
    if info_fn is None:
        return None
    try:
        info = info_fn()
    except Exception:  # noqa: BLE001 - health endpoint must never raise on this
        return None
    if info is None:
        return None
    build = getattr(info, "build", None)
    return int(build) if build is not None else None


# ---------------------------------------------------------------------------
# available_range probing — mirrors MetaTraderClient (call under the MT5 lock).
# ---------------------------------------------------------------------------
def _probe_latest_bar(symbol: str, mt5_timeframe: int) -> datetime | None:
    rates = mt5.copy_rates_from_pos(symbol, mt5_timeframe, 0, 1)
    if rates is None or len(rates) == 0:
        return None
    return _epoch_to_naive(int(rates[0]["time"]))


def _probe_earliest_bar(symbol: str, mt5_timeframe: int) -> datetime | None:
    rates = mt5.copy_rates_from(symbol, mt5_timeframe, _HISTORY_ANCHOR, 1)
    if rates is not None and len(rates) > 0:
        return _epoch_to_naive(int(rates[0]["time"]))

    earliest: datetime | None = None
    cursor = _HISTORY_ANCHOR
    now = datetime.now()
    chunk_days = _get_chunk_days(mt5_timeframe)

    while cursor < now:
        chunk_end = min(cursor + timedelta(days=chunk_days), now)
        if chunk_end <= cursor:
            chunk_end = now

        rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
        if rates is not None and len(rates) > 0:
            chunk_earliest = _epoch_to_naive(int(rates[0]["time"]))
            earliest = (
                chunk_earliest if earliest is None else min(earliest, chunk_earliest)
            )

        if chunk_end >= now:
            break
        cursor = chunk_end + timedelta(seconds=1)

    return earliest


def _count_bars_between(
    symbol: str, mt5_timeframe: int, start: datetime, end: datetime
) -> int:
    total = 0
    cursor = start
    chunk_days = _get_chunk_days(mt5_timeframe)

    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
        if rates is not None and len(rates) > 0:
            total += len(rates)
            cursor = _epoch_to_naive(int(rates[-1]["time"])) + timedelta(seconds=1)
        else:
            cursor = chunk_end + timedelta(seconds=1)

        if cursor > end:
            break

    return total


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> GatewayApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        try:
            self._check_token()
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            handler = self._ROUTES.get(parsed.path)
            if handler is None:
                raise GatewayError(
                    404, "not_found", f"Unknown endpoint '{parsed.path}'."
                )
            handler(self, params)
        except GatewayError as exc:
            self._send_json(exc.status, {"error": exc.message, "code": exc.code})
        except BrokenPipeError:
            logger.debug("client disconnected before response was sent")
        except Exception as exc:  # noqa: BLE001 - top-level guard: report, never crash
            logger.exception("unhandled error serving %s", self.path)
            self._send_json(500, {"error": str(exc), "code": "internal_error"})

    # -- routing ------------------------------------------------------------
    def _route_health(self, _params) -> None:
        self._send_json(200, self.app.health())

    def _route_symbol_info(self, params) -> None:
        symbol = _require_param(params, "symbol")
        self._send_json(200, self.app.symbol_info(symbol))

    def _route_symbols_search(self, params) -> None:
        query = _require_param(params, "query")
        self._send_json(200, self.app.search_symbols(query))

    def _route_available_range(self, params) -> None:
        symbol = _require_param(params, "symbol")
        timeframe = _require_param(params, "timeframe")
        self._send_json(200, self.app.available_range(symbol, timeframe))

    def _route_ohlcv(self, params) -> None:
        symbol = _require_param(params, "symbol")
        timeframe = _require_param(params, "timeframe")
        start = _parse_dt(_require_param(params, "start"), "start")
        end = _parse_dt(_require_param(params, "end"), "end")
        self._send_npz(self.app.ohlcv(symbol, timeframe, start, end))

    def _route_ticks(self, params) -> None:
        symbol = _require_param(params, "symbol")
        start = _parse_dt(_require_param(params, "start"), "start")
        end = _parse_dt(_require_param(params, "end"), "end")
        flags = _resolve_tick_flags(_optional_param(params, "flags"))
        self._send_npz(self.app.ticks(symbol, start, end, flags))

    _ROUTES = {
        "/v1/health": _route_health,
        "/v1/symbol_info": _route_symbol_info,
        "/v1/symbols/search": _route_symbols_search,
        "/v1/available_range": _route_available_range,
        "/v1/ohlcv": _route_ohlcv,
        "/v1/ticks": _route_ticks,
    }

    # -- helpers ------------------------------------------------------------
    def _check_token(self) -> None:
        token = self.app.token
        if token is None:
            return
        if self.headers.get("X-Gateway-Token") != token:
            raise GatewayError(
                401, "unauthorized", "Missing or invalid X-Gateway-Token header."
            )

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_npz(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _require_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name)
    if not values or not values[0]:
        raise GatewayError(
            400, "missing_parameter", f"Missing required query parameter '{name}'."
        )
    return values[0]


def _optional_param(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values or not values[0]:
        return None
    return values[0]


def _parse_dt(value: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise GatewayError(
            400,
            "invalid_datetime",
            f"Invalid ISO-8601 datetime for '{name}': {value!r}.",
        ) from exc


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: GatewayApp):
        super().__init__(address, _GatewayHandler)
        self.app = app


def build_server(host: str, port: int, token: str | None = None) -> GatewayServer:
    """Create (but do not start) a gateway server. Tests use ``port=0``."""
    return GatewayServer((host, port), GatewayApp(token=token))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MT5 remote market-data gateway.")
    parser.add_argument(
        "--host",
        default=os.environ.get("MT5_GATEWAY_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MT5_GATEWAY_PORT", "18812")),
        help="Bind port (default: 18812).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MT5_GATEWAY_TOKEN"),
        help="Optional shared secret required in the X-Gateway-Token header.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = build_server(args.host, args.port, token=args.token)
    app: GatewayApp = server.app

    if app.try_initialize():
        logger.info("Connected to MT5 terminal at startup.")
    else:
        logger.warning(
            "MT5 not initialized at startup; will retry lazily per request."
        )

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s; shutting down.", signum)
        # shutdown() must run off the serve_forever thread to avoid deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "MT5 gateway listening on %s:%s (schema %s)",
        args.host,
        args.port,
        SCHEMA_VERSION,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.shutdown_mt5()


if __name__ == "__main__":
    main()
