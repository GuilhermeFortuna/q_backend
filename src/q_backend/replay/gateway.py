"""HTTP market gateway of the replay simulator: the existing ``/v1/`` wire contract, served causally.

Every request captures one visibility watermark at ingress and answers from the immutable
``ReplayMarket``; nothing in a request can move the market or see past the watermark. The
endpoints, parameters, NPZ arrays and metadata mirror ``gateway/mt5_gateway.py``, so the real
``RemoteMt5Client`` consumes them unchanged. ``/v1/clock`` (contract ``edge/clock/snapshot``)
additionally reports the canonical market clock.
"""

from __future__ import annotations

import io
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from q_backend.replay.market import (
    COPY_TICKS_ALL,
    MarketError,
    ReplayMarket,
    naive_local_to_utc_ms,
    naive_local_to_wire_seconds,
)

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Ingress:
    """What a request sees: the visibility watermark and the canonical clock at ingress."""

    visible_us: int
    clock: dict[str, Any]


def _npz(arrays: dict[str, np.ndarray], metadata: dict[str, Any] | None = None) -> Response:
    payload = dict(arrays)
    if metadata is not None:
        payload["metadata"] = np.array(json.dumps(metadata))
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **payload)
    return Response(content=buffer.getvalue(), media_type="application/octet-stream")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "code": code})


def create_gateway_app(
    *,
    market: ReplayMarket,
    ingress: Callable[[], Ingress],
    token: str | None = None,
) -> FastAPI:
    """Build the gateway app. ``ingress`` is called once per request and must not block."""
    app = FastAPI(title="Q replay market gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(MarketError)
    async def _market_error(_: Request, exc: MarketError) -> JSONResponse:
        return _error(exc.status, exc.code, str(exc))

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if token is not None:
            supplied = request.headers.get("X-Gateway-Token", "")
            if not secrets.compare_digest(supplied, token):
                return _error(401, "unauthorized", "missing or invalid gateway token")
        return await call_next(request)

    def _snapshot() -> Ingress:
        return ingress()

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "mt5_connected": True,
            "terminal_build": None,
        }

    @app.get("/v1/clock")
    async def clock() -> dict[str, Any]:
        return dict(_snapshot().clock)

    @app.get("/v1/symbol_info")
    async def symbol_info(symbol: str = Query(...)):
        return market.symbol_info(symbol)

    @app.get("/v1/symbols/search")
    async def search(query: str = Query("")):
        return market.search_symbols(query)

    @app.get("/v1/available_range")
    async def available_range(symbol: str = Query(...), timeframe: str = Query(...)):
        found = market.available_range(symbol, timeframe, _snapshot().visible_us)
        if found is None:
            return _error(404, "range_unavailable", f"no visible bars for {symbol} {timeframe}")
        return found

    @app.get("/v1/ohlcv")
    async def ohlcv(
        symbol: str = Query(...),
        timeframe: str = Query(...),
        start: str = Query(...),
        end: str = Query(...),
    ) -> Response:
        visible = _snapshot().visible_us
        bars, forming = market.ohlcv(
            symbol,
            timeframe,
            start_wire_s=naive_local_to_wire_seconds(start),
            end_wire_s=naive_local_to_wire_seconds(end),
            visible_us=visible,
        )
        return _npz(bars, {"truncated": False, "max_bars": 0, "forming_time": forming})

    @app.get("/v1/ohlcv/recent")
    async def ohlcv_recent(
        symbol: str = Query(...),
        timeframe: str = Query(...),
        count: int = Query(..., ge=1, le=5000),
    ) -> Response:
        bars, _ = market.recent_ohlcv(symbol, timeframe, count, _snapshot().visible_us)
        return _npz(bars)

    @app.get("/v1/ticks")
    async def ticks(
        symbol: str = Query(...),
        start: str = Query(...),
        end: str = Query(...),
        flags: str = Query(COPY_TICKS_ALL),
    ) -> Response:
        columns = market.ticks(
            symbol,
            start_utc_ms=naive_local_to_utc_ms(start, market.zone),
            end_utc_ms=naive_local_to_utc_ms(end, market.zone),
            flags=flags,
            visible_us=_snapshot().visible_us,
        )
        return _npz(columns)

    @app.get("/v1/ticks/page")
    async def ticks_page(
        symbol: str = Query(...),
        start_msc: int = Query(...),
        end_msc: int = Query(...),
        limit: int = Query(1_000_000),
    ) -> Response:
        columns, meta = market.ticks_page(
            symbol,
            start_wire=start_msc,
            end_wire=end_msc,
            limit=limit,
            visible_us=_snapshot().visible_us,
        )
        return _npz(columns, meta)

    @app.get("/v1/trades")
    async def trades(
        symbol: str = Query(...),
        start_utc: str = Query(...),
        end_utc: str = Query(...),
    ) -> Response:
        start = _aware_utc_ms(start_utc)
        end = _aware_utc_ms(end_utc)
        result = market.trades(symbol, start_utc_ms=start, end_utc_ms=end, visible_us=_snapshot().visible_us)
        return _npz(result["columns"], result["metadata"])

    return app


def _aware_utc_ms(text: str) -> int:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketError(400, "invalid_datetime", f"invalid datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        raise MarketError(400, "invalid_datetime", "trades takes timezone-aware UTC datetimes")
    return int(parsed.timestamp() * 1000)


class ServerThread:
    """Run one ASGI app on a loopback port in a background thread (tests and the simulator)."""

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 0) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("gateway server failed to start")
            self._thread.join(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
