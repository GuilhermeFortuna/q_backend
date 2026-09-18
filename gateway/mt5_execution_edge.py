"""MT5 execution edge (Q-040).

Stdlib-only HTTP server beside the MT5 terminal under Wine. Implements the
execution-edge wire contract: quote, check, submit, lookup, positions, deals,
and account. At most one ``order_send`` per ``intent_id`` per process lifetime;
no retries and no inline recovery on ambiguous sends.

Dependencies: stdlib + ``MetaTrader5`` + ``numpy`` only. Never imports
``q_backend``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import MetaTrader5 as mt5

SCHEMA_VERSION = "1.0"
SCHEMA_MAJOR = 1
_SCHEMA_HEADER = "X-Schema-Major"
_DEFAULT_DEVIATION = 20
_MAGIC_BASE = 0

logger = logging.getLogger("mt5_execution_edge")


class EdgeError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def intent_magic(intent_id: str, *, base: int = _MAGIC_BASE) -> int:
    order_uuid = UUID(intent_id)
    return int((base ^ (order_uuid.int & 0x7FFFFFFF)) & 0x7FFFFFFF)


def intent_comment(intent_id: str) -> str:
    compact = str(UUID(intent_id)).replace("-", "")[:24]
    return f"q:{compact}"


class IntentTable:
    """In-memory intent registry scoped to the process lifetime."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, intent_id: str) -> bool:
        with self._lock:
            if intent_id in self._seen:
                return False
            self._seen.add(intent_id)
            return True


def _is_success_retcode(retcode: int) -> bool:
    return int(retcode) in {int(mt5.TRADE_RETCODE_DONE), int(mt5.TRADE_RETCODE_DONE_PARTIAL)}


def _is_unknown_retcode(retcode: int) -> bool:
    return int(retcode) in {int(mt5.TRADE_RETCODE_TIMEOUT), int(mt5.TRADE_RETCODE_NO_CONNECTION)}


def _retcode_label(retcode: int) -> str:
    labels = {
        int(mt5.TRADE_RETCODE_DONE): "done",
        int(mt5.TRADE_RETCODE_DONE_PARTIAL): "done_partial",
        int(mt5.TRADE_RETCODE_TIMEOUT): "timeout",
        int(mt5.TRADE_RETCODE_NO_CONNECTION): "no_connection",
        int(mt5.TRADE_RETCODE_REJECT): "reject",
    }
    return labels.get(int(retcode), f"retcode_{retcode}")


def _normalize_volume(volume: float, *, step: float, vmin: float, vmax: float) -> float:
    step_value = float(step)
    value = float(volume)
    if step_value > 0:
        steps = round(value / step_value)
        value = steps * step_value
    if value < vmin:
        raise EdgeError(400, "invalid_request", "volume below symbol minimum")
    if vmax > 0 and value > vmax:
        raise EdgeError(400, "invalid_request", "volume above symbol maximum")
    return value


def _normalize_price(price: float, digits: int) -> float:
    return round(float(price), int(digits))


def _select_filling_mode(symbol_filling_mode: int) -> int:
    mode = int(symbol_filling_mode)
    if mode & int(mt5.SYMBOL_FILLING_FOK):
        return int(mt5.ORDER_FILLING_FOK)
    if mode & int(mt5.SYMBOL_FILLING_IOC):
        return int(mt5.ORDER_FILLING_IOC)
    if mode & int(mt5.SYMBOL_FILLING_RETURN):
        return int(mt5.ORDER_FILLING_RETURN)
    raise EdgeError(400, "invalid_request", "symbol does not advertise a supported filling mode")


def _map_side_to_mt5(side: str) -> int:
    return int(mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL)


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    if isinstance(obj, dict):
        return dict(obj)
    return dict(vars(obj))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _safe_last_error() -> tuple[int, str]:
    last_error = getattr(mt5, "last_error", None)
    if last_error is None:
        return (0, "unknown")
    try:
        code, desc = last_error()
        return int(code), str(desc)
    except Exception:  # noqa: BLE001 - diagnostic only
        return (0, "unknown")


def _terminal_build() -> int | None:
    info_fn = getattr(mt5, "terminal_info", None)
    if info_fn is None:
        return None
    try:
        info = info_fn()
    except Exception:  # noqa: BLE001 - health must never raise
        return None
    if info is None:
        return None
    build = getattr(info, "build", None)
    return int(build) if build is not None else None


def _parse_window(value: str | int, name: str) -> datetime:
    if isinstance(value, bool):
        raise EdgeError(400, "invalid_request", f"Invalid window value for '{name}'.")
    if isinstance(value, int):
        seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise EdgeError(400, "invalid_request", f"Invalid window value for '{name}'.") from exc
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    raise EdgeError(400, "invalid_request", f"Invalid window value for '{name}'.")


def _deal_matches_intent(deal: Any, *, magic: int, comment: str) -> bool:
    plain = _to_plain_dict(deal)
    deal_magic = int(plain.get("magic", 0))
    deal_comment = str(plain.get("comment", ""))
    return deal_magic == magic and deal_comment.startswith(comment)


def _serialize_deal(deal: Any) -> dict[str, Any]:
    plain = _to_plain_dict(deal)
    payload: dict[str, Any] = {
        "ticket": int(plain["ticket"]),
        "order_ticket": int(plain.get("order", plain.get("order_ticket", 0))),
        "symbol": str(plain["symbol"]),
        "volume": float(plain["volume"]),
        "price": float(plain["price"]),
    }
    for key in ("type", "entry", "commission", "swap", "profit", "fee", "magic", "comment", "time_msc"):
        if key in plain and plain[key] is not None:
            payload[key] = plain[key]
    return payload


def _serialize_position(position: Any) -> dict[str, Any]:
    plain = _to_plain_dict(position)
    payload: dict[str, Any] = {
        "ticket": int(plain["ticket"]),
        "symbol": str(plain["symbol"]),
        "type": int(plain["type"]),
        "volume": float(plain["volume"]),
        "price_open": float(plain["price_open"]),
    }
    for key in ("sl", "tp", "price_current", "profit", "magic", "comment", "time"):
        if key in plain and plain[key] is not None:
            payload[key] = plain[key]
    return payload


def _validate_order_fields(order: dict[str, Any]) -> None:
    if order.get("sl") is not None or order.get("tp") is not None:
        raise EdgeError(400, "invalid_request", "stop-loss and take-profit are not supported on submit")
    order_type = order.get("type")
    if order_type is not None and int(order_type) not in {int(mt5.ORDER_TYPE_BUY), int(mt5.ORDER_TYPE_SELL)}:
        raise EdgeError(400, "invalid_request", "only market buy/sell orders are supported")
    type_time = order.get("type_time")
    if type_time is not None and int(type_time) != int(mt5.ORDER_TIME_GTC):
        raise EdgeError(400, "invalid_request", "only GTC market orders are supported")


def _resolve_order_type(order: dict[str, Any]) -> int:
    if order.get("type") is not None:
        return int(order["type"])
    side = order.get("side")
    if side is None:
        raise EdgeError(400, "invalid_request", "order side or type is required")
    return _map_side_to_mt5(str(side))


class EdgeApp:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._intents = IntentTable()

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
            raise EdgeError(
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

    def health(self) -> dict[str, Any]:
        with self._lock:
            connected = self._ensure_initialized()
            build = _terminal_build() if connected else None
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "mt5_connected": connected,
            "terminal_build": build,
        }

    def account(self) -> dict[str, Any]:
        with self._lock:
            self._require_ready()
            info = mt5.account_info()
            if info is None:
                raise EdgeError(
                    503,
                    "mt5_unavailable",
                    "MetaTrader 5 terminal is not initialized.",
                )
            terminal = mt5.terminal_info()
            plain = _to_plain_dict(info)
            return {
                "login": int(plain["login"]),
                "server": str(plain["server"]),
                "currency": str(plain["currency"]),
                "trade_allowed": bool(plain.get("trade_allowed", False)),
                "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
                "balance": float(plain["balance"]),
                "equity": float(plain["equity"]),
                "margin_free": float(plain["margin_free"]),
            }

    def quote(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise EdgeError(404, "symbol_not_found", f"Symbol '{symbol}' is not selectable.")
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise EdgeError(503, "mt5_unavailable", f"Quote for '{symbol}' is unavailable.")
            plain = _to_plain_dict(tick)
            time_msc = int(plain.get("time_msc", int(plain.get("time", 0)) * 1000))
            now_msc = int(time.time() * 1000)
            age_ms = max(0, now_msc - time_msc)
            return {
                "symbol": symbol,
                "bid": float(plain["bid"]),
                "ask": float(plain["ask"]),
                "last": float(plain.get("last", plain["bid"])),
                "time_msc": time_msc,
                "age_ms": age_ms,
            }

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._build_mt5_request(payload, claim_intent=False)
        with self._lock:
            self._require_ready()
            result = mt5.order_check(request)
            if result is None:
                code, desc = _safe_last_error()
                return {
                    "allowed": False,
                    "retcode": int(code),
                    "margin": 0.0,
                    "reason": f"order_check failed: {desc}",
                }
            plain = _to_plain_dict(result)
            retcode = int(plain.get("retcode", 0))
            allowed = _is_success_retcode(retcode)
            response = {
                "allowed": allowed,
                "retcode": retcode,
                "margin": float(plain.get("margin", 0.0)),
            }
            if not allowed:
                response["reason"] = f"order_check rejected: {_retcode_label(retcode)}"
            return response

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent_id = str(payload["intent_id"])
        if not self._intents.claim(intent_id):
            raise EdgeError(400, "duplicate_intent", f"Intent '{intent_id}' was already submitted.")
        request = self._build_mt5_request(payload, claim_intent=False)
        with self._lock:
            self._require_ready()
            try:
                send = mt5.order_send(request)
            except Exception as exc:  # noqa: BLE001 - ambiguous send must surface indeterminate
                logger.exception("order_send raised for intent %s", intent_id)
                return {
                    "outcome": "indeterminate",
                    "reason": f"order_send raised: {exc}",
                }
            if send is None:
                code, desc = _safe_last_error()
                return {
                    "outcome": "indeterminate",
                    "reason": f"order_send returned None: {desc} (code {code})",
                }
            plain = _to_plain_dict(send)
            retcode = int(plain.get("retcode", 0))
            if _is_unknown_retcode(retcode):
                return {
                    "outcome": "indeterminate",
                    "reason": f"ambiguous send outcome: {_retcode_label(retcode)}",
                }
            if not _is_success_retcode(retcode):
                return {
                    "outcome": "rejected",
                    "retcode": retcode,
                    "reason": f"order_send rejected: {_retcode_label(retcode)}",
                }
            return {
                "outcome": "accepted",
                "order_ticket": int(plain.get("order", 0)),
                "retcode": retcode,
            }

    def lookup(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent_id = str(payload["intent_id"])
        magic = int(payload.get("magic", intent_magic(intent_id)))
        comment = intent_comment(intent_id)
        window_start = _parse_window(payload["window_start"], "window_start")
        window_end = _parse_window(payload["window_end"], "window_end")

        with self._lock:
            if not self._ensure_initialized():
                return {
                    "outcome": "unavailable",
                    "reason": "MetaTrader 5 terminal is not initialized.",
                    "closes_intent": False,
                }
            deals = mt5.history_deals_get(window_start, window_end)
            if deals is None:
                return {
                    "outcome": "unavailable",
                    "reason": "Terminal deal history is unavailable.",
                    "closes_intent": False,
                }

            matched_deals = [deal for deal in deals if _deal_matches_intent(deal, magic=magic, comment=comment)]
            if matched_deals:
                return {
                    "outcome": "filled",
                    "deals": [_serialize_deal(deal) for deal in matched_deals],
                    "closes_intent": True,
                }

            history_orders = mt5.history_orders_get(window_start, window_end) or []
            rejected_states = {int(mt5.ORDER_STATE_CANCELED), int(mt5.ORDER_STATE_REJECTED)}
            for order in history_orders:
                plain = _to_plain_dict(order)
                if int(plain.get("magic", 0)) != magic:
                    continue
                if not str(plain.get("comment", "")).startswith(comment):
                    continue
                if int(plain.get("state", 0)) in rejected_states:
                    outcome: dict[str, Any] = {
                        "outcome": "rejected",
                        "retcode": int(mt5.TRADE_RETCODE_REJECT),
                        "closes_intent": True,
                    }
                    outcome["reason"] = "order rejected or cancelled in terminal history"
                    return outcome

        return {"outcome": "not_found", "closes_intent": True}

    def positions(self, symbol: str | None) -> list[dict[str, Any]]:
        with self._lock:
            self._require_ready()
            rows = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
            if rows is None:
                return []
            return [_serialize_position(row) for row in rows]

    def deals(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        window_start = _parse_window(payload["window_start"], "window_start")
        window_end = _parse_window(payload["window_end"], "window_end")
        magic = payload.get("magic")
        symbol = payload.get("symbol")
        with self._lock:
            self._require_ready()
            rows = mt5.history_deals_get(window_start, window_end)
            if rows is None:
                raise EdgeError(503, "mt5_unavailable", "Terminal deal history is unavailable.")
            filtered = []
            for row in rows:
                plain = _to_plain_dict(row)
                if magic is not None and int(plain.get("magic", 0)) != int(magic):
                    continue
                if symbol is not None and str(plain.get("symbol")) != str(symbol):
                    continue
                filtered.append(_serialize_deal(row))
            return filtered

    def _build_mt5_request(self, payload: dict[str, Any], *, claim_intent: bool) -> dict[str, Any]:
        intent_id = str(payload["intent_id"])
        order = dict(payload["order"])
        expected_magic = intent_magic(intent_id)
        expected_comment = intent_comment(intent_id)
        if "magic" in order and int(order["magic"]) != expected_magic:
            raise EdgeError(400, "intent_field_mismatch", "Supplied magic does not match intent derivation.")
        if "comment" in order and str(order["comment"]) != expected_comment:
            raise EdgeError(400, "intent_field_mismatch", "Supplied comment does not match intent derivation.")
        _validate_order_fields(order)

        symbol = str(order["symbol"])
        side_type = _resolve_order_type(order)
        with self._lock:
            self._require_ready()
            if not mt5.symbol_select(symbol, True):
                raise EdgeError(404, "symbol_not_found", f"Symbol '{symbol}' is not selectable.")
            info = mt5.symbol_info(symbol)
            if info is None:
                raise EdgeError(404, "symbol_not_found", f"No symbol_info for '{symbol}'.")
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise EdgeError(503, "mt5_unavailable", f"Quote for '{symbol}' is unavailable.")
            info_plain = _to_plain_dict(info)
            tick_plain = _to_plain_dict(tick)
            volume = _normalize_volume(
                float(order["volume"]),
                step=float(info_plain["volume_step"]),
                vmin=float(info_plain["volume_min"]),
                vmax=float(info_plain["volume_max"]),
            )
            filling = _select_filling_mode(int(info_plain["filling_mode"]))
            if order.get("price") is not None:
                price = _normalize_price(float(order["price"]), int(info_plain["digits"]))
            else:
                side_price = float(tick_plain["ask"] if side_type == int(mt5.ORDER_TYPE_BUY) else tick_plain["bid"])
                price = _normalize_price(side_price, int(info_plain["digits"]))

        return {
            "action": int(mt5.TRADE_ACTION_DEAL),
            "symbol": symbol,
            "volume": volume,
            "type": side_type,
            "price": price,
            "deviation": int(order.get("deviation", _DEFAULT_DEVIATION)),
            "magic": expected_magic,
            "comment": expected_comment,
            "type_time": int(mt5.ORDER_TIME_GTC),
            "type_filling": filling,
        }


class _EdgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> EdgeApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            route = self._ROUTES.get((method, parsed.path))
            if route is None:
                raise EdgeError(404, "not_found", f"Unknown endpoint '{parsed.path}'.")
            if parsed.path != "/v1/health":
                self._check_schema_major()
            params = parse_qs(parsed.query)
            body = self._read_json_body() if method == "POST" else {}
            route(self, params, body)
        except EdgeError as exc:
            self._send_json(exc.status, {"error": exc.message, "code": exc.code})
        except BrokenPipeError:
            logger.debug("client disconnected before response was sent")
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.exception("unhandled error serving %s", self.path)
            self._send_json(500, {"error": str(exc), "code": "internal_error"})

    def _check_schema_major(self) -> None:
        raw = self.headers.get(_SCHEMA_HEADER)
        if raw is None:
            return
        try:
            major = int(raw)
        except ValueError as exc:
            raise EdgeError(400, "schema_major_mismatch", f"Invalid {_SCHEMA_HEADER} header.") from exc
        if major != SCHEMA_MAJOR:
            raise EdgeError(
                400,
                "schema_major_mismatch",
                f"Unsupported schema major {major}; this edge serves major {SCHEMA_MAJOR}.",
            )

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise EdgeError(400, "invalid_request", "Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise EdgeError(400, "invalid_request", "Request body must be a JSON object.")
        return payload

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route_health(self, _params, _body) -> None:
        self._send_json(200, self.app.health())

    def _route_quote(self, params, _body) -> None:
        symbol = _require_param(params, "symbol")
        self._send_json(200, self.app.quote(symbol))

    def _route_account(self, _params, _body) -> None:
        self._send_json(200, self.app.account())

    def _route_positions(self, params, _body) -> None:
        symbol = _optional_param(params, "symbol")
        self._send_json(200, self.app.positions(symbol))

    def _route_check(self, _params, body) -> None:
        self._send_json(200, self.app.check(body))

    def _route_submit(self, _params, body) -> None:
        self._send_json(200, self.app.submit(body))

    def _route_lookup(self, _params, body) -> None:
        self._send_json(200, self.app.lookup(body))

    def _route_deals(self, _params, body) -> None:
        self._send_json(200, self.app.deals(body))

    _ROUTES = {
        ("GET", "/v1/health"): _route_health,
        ("GET", "/v1/quote"): _route_quote,
        ("GET", "/v1/account"): _route_account,
        ("GET", "/v1/positions"): _route_positions,
        ("POST", "/v1/check"): _route_check,
        ("POST", "/v1/submit"): _route_submit,
        ("POST", "/v1/lookup"): _route_lookup,
        ("POST", "/v1/deals"): _route_deals,
    }


def _require_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name)
    if not values or not values[0]:
        raise EdgeError(400, "invalid_request", f"Missing required query parameter '{name}'.")
    return values[0]


def _optional_param(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values or not values[0]:
        return None
    return values[0]


class EdgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: EdgeApp):
        super().__init__(address, _EdgeHandler)
        self.app = app


def build_server(host: str, port: int) -> EdgeServer:
    return EdgeServer((host, port), EdgeApp())


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 execution edge.")
    parser.add_argument(
        "--host",
        default=os.environ.get("MT5_EDGE_HOST", "127.0.0.1"),
        help="Bind address (loopback only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MT5_EDGE_PORT", "18813")),
        help="Bind port (default: 18813).",
    )
    args = parser.parse_args(argv)

    if not _is_loopback(args.host):
        print(f"refusing to bind execution edge to non-loopback host {args.host!r}", file=sys.stderr)
        return 78

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = build_server(args.host, args.port)
    app: EdgeApp = server.app

    if app.try_initialize():
        logger.info("Connected to MT5 terminal at startup.")
    else:
        logger.warning("MT5 not initialized at startup; will retry lazily per request.")

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s; shutting down.", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "MT5 execution edge listening on %s:%s (schema %s)",
        args.host,
        args.port,
        SCHEMA_VERSION,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.shutdown_mt5()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
