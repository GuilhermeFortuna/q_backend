"""In-process HTTP fake of the MT5 execution edge contract (Q-042)."""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from uuid import UUID

SCHEMA_VERSION = "1.0"


@dataclass
class FakeEdgeState:
    schema_version: str = SCHEMA_VERSION
    mt5_connected: bool = True
    terminal_build: int | None = 4200
    account: dict[str, Any] = field(
        default_factory=lambda: {
            "login": 12345678,
            "server": "Broker-Demo",
            "currency": "BRL",
            "trade_allowed": True,
            "terminal_trade_allowed": True,
            "balance": 100000.0,
            "equity": 100000.0,
            "margin_free": 100000.0,
        }
    )
    quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    submit_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(default_factory=dict)
    default_submit: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | None = None
    lookup_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(default_factory=dict)
    default_lookup: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | None = None
    submitted_intents: set[str] = field(default_factory=set)
    submit_delay_s: float = 0.0
    drop_connection_on_submit: bool = False
    health_status: int = 200
    unavailable: bool = False
    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)


def _default_quote(symbol: str) -> dict[str, Any]:
    now_msc = int(time.time() * 1000)
    return {
        "symbol": symbol,
        "bid": 130000.0,
        "ask": 130010.0,
        "last": 130005.0,
        "time_msc": now_msc,
        "age_ms": 50,
    }


def _accepted_submit(_payload: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": "accepted", "order_ticket": 9001, "retcode": 10009}


class _FakeEdgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> FakeEdgeState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json() if method == "POST" else None
        self.state.requests.append((method, path, body))

        if self.state.unavailable and path != "/v1/health":
            self._send_json(503, {"error": "edge unavailable", "code": "mt5_unavailable"})
            return

        if path == "/v1/health" and method == "GET":
            if self.state.health_status != 200:
                self._send_raw(self.state.health_status, b"")
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "schema_version": self.state.schema_version,
                    "mt5_connected": self.state.mt5_connected,
                    "terminal_build": self.state.terminal_build,
                },
            )
            return

        if path == "/v1/account" and method == "GET":
            self._send_json(200, dict(self.state.account))
            return

        if path == "/v1/quote" and method == "GET":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            quote = self.state.quotes.get(symbol) or _default_quote(symbol)
            self._send_json(200, quote)
            return

        if path == "/v1/submit" and method == "POST":
            if self.state.drop_connection_on_submit:
                self.close_connection = True
                return
            if self.state.submit_delay_s > 0:
                time.sleep(self.state.submit_delay_s)
            intent_id = str(body.get("intent_id", ""))
            if intent_id in self.state.submitted_intents:
                self._send_json(
                    400,
                    {
                        "error": f"Intent '{intent_id}' was already submitted.",
                        "code": "duplicate_intent",
                    },
                )
                return
            self.state.submitted_intents.add(intent_id)
            handler = self.state.submit_handlers.get(intent_id)
            if handler is not None:
                outcome = handler(body)
            elif callable(self.state.default_submit):
                outcome = self.state.default_submit(body)
            elif self.state.default_submit is not None:
                outcome = self.state.default_submit
            else:
                outcome = _accepted_submit(body)
            self._send_json(200, outcome)
            return

        if path == "/v1/lookup" and method == "POST":
            intent_id = str(body.get("intent_id", ""))
            handler = self.state.lookup_handlers.get(intent_id)
            if handler is not None:
                outcome = handler(body)
            elif callable(self.state.default_lookup):
                outcome = self.state.default_lookup(body)
            elif self.state.default_lookup is not None:
                outcome = self.state.default_lookup
            else:
                outcome = {"outcome": "not_found", "closes_intent": True}
            self._send_json(200, outcome)
            return

        if path == "/v1/check" and method == "POST":
            self._send_json(
                200,
                {"allowed": True, "retcode": 0, "margin": 0.0},
            )
            return

        if path == "/v1/positions" and method == "GET":
            self._send_json(200, [])
            return

        if path == "/v1/deals" and method == "POST":
            self._send_json(200, [])
            return

        self._send_json(404, {"error": "not found", "code": "not_found"})

    def _send_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_raw(status, body, content_type="application/json")

    def _send_raw(self, status: int, body: bytes, *, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Schema-Major", "1")
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def fake_edge_server(state: FakeEdgeState | None = None):
    state = state or FakeEdgeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEdgeHandler)
    server.state = state
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def intent_magic(intent_id: UUID, *, base: int = 0) -> int:
    return int((base ^ (intent_id.int & 0x7FFFFFFF)) & 0x7FFFFFFF)


def intent_comment(intent_id: UUID) -> str:
    compact = str(intent_id).replace("-", "")[:24]
    return f"q:{compact}"


def make_filled_lookup_deal(
    *,
    ticket: int,
    symbol: str,
    volume: float,
    price: float,
    order_ticket: int = 9001,
    magic: int | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticket": ticket,
        "order_ticket": order_ticket,
        "symbol": symbol,
        "volume": volume,
        "price": price,
    }
    if magic is not None:
        payload["magic"] = magic
    if comment is not None:
        payload["comment"] = comment
    return payload


class _ResettingSocket(socket.socket):
    """Socket that raises ConnectionResetError once (transport-error tests)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reset_next_send = False

    def sendall(self, data, *args, **kwargs):
        if self._reset_next_send:
            self._reset_next_send = False
            raise ConnectionResetError("connection reset by peer")
        return super().sendall(data, *args, **kwargs)


def resetting_transport() -> Any:
    """httpx transport that resets the connection on the first POST to /v1/submit."""

    import httpx

    class _Transport(httpx.BaseTransport):
        def __init__(self) -> None:
            self._inner = httpx.HTTPTransport()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/v1/submit"):
                raise httpx.ConnectError("connection reset by peer", request=request)
            return self._inner.handle_request(request)

    return _Transport()
