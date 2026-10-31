"""HTTP broker edge of the replay simulator: the existing execution-edge contract, simulated.

Routes, bodies and reply shapes follow ``schema/edge/execution.yaml`` (edge 1.2), so the existing
``EdgeClient`` and ``EdgeBroker`` drive the simulator unchanged. Intent semantics are the contract's:
a second submission with a known ``intent_id`` is refused with HTTP 400 ``duplicate_intent`` and
never reaches the broker again. Submissions answer at their visibility instant; reads answer the
state at ingress, delivered after the configured response delay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from q_backend.replay.broker import DuplicateIntentError, SimulatedBroker
from q_backend.replay.service import ReplaySimulator, SubmitTimeoutError

EDGE_SCHEMA_VERSION = "1.2"
SUBMIT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 30.0


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "code": code})


def _window_us(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value) * 1_000_000
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def _submission(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Normalize the three body shapes into (intent_id, action, flat body)."""
    intent_id = str(body.get("intent_id", ""))
    if not intent_id:
        raise ValueError("intent_id is required")
    action = body.get("action") or "deal"
    if action in ("deal", "pending", "check"):
        order = body.get("order")
        if not isinstance(order, dict):
            raise ValueError("order is required")
        return intent_id, action, dict(order)
    return intent_id, action, {key: value for key, value in body.items() if key not in ("intent_id", "action")}


def create_edge_app(simulator: ReplaySimulator) -> FastAPI:
    app = FastAPI(title="Q replay broker edge", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "schema_version": EDGE_SCHEMA_VERSION, "mt5_connected": True, "terminal_build": None}

    @app.get("/v1/account")
    async def account() -> Any:
        return await _read(simulator, lambda ingress: simulator.broker().account(ingress))

    @app.get("/v1/quote")
    async def quote(symbol: str = Query(...)) -> Any:
        found = await _read(simulator, lambda ingress: simulator.broker().quote(symbol, ingress))
        if found is None:
            return _error(404, "quote_unavailable", f"no priced quote for {symbol}")
        return found

    @app.get("/v1/positions")
    async def positions(symbol: str | None = Query(None)) -> Any:
        return await _read(simulator, lambda ingress: simulator.broker().positions(ingress, symbol))

    @app.get("/v1/orders")
    async def orders(symbol: str | None = Query(None)) -> Any:
        return await _read(simulator, lambda ingress: simulator.broker().orders(ingress, symbol))

    @app.post("/v1/deals")
    async def deals(request: Request) -> Any:
        body = await request.json()
        start = _window_us(body["window_start"])
        end = _window_us(body["window_end"])
        symbol = body.get("symbol")
        return await _read(simulator, lambda ingress: simulator.broker().deals(ingress, start, end, symbol))

    @app.post("/v1/lookup")
    async def lookup(request: Request) -> Any:
        body = await request.json()
        intent_id = str(body.get("intent_id", ""))
        kind = body.get("kind") or "deal"
        return await _read(simulator, lambda ingress: simulator.broker().lookup(intent_id, kind, ingress))

    @app.post("/v1/check")
    async def check(request: Request) -> Any:
        body = await request.json()
        try:
            intent_id, action, flat = _submission({**body, "action": "check"})
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        return await _submit(simulator, intent_id, action, flat, strip_closes=True)

    @app.post("/v1/submit")
    async def submit(request: Request) -> Any:
        body = await request.json()
        try:
            intent_id, action, flat = _submission(body)
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        if action not in ("deal", "pending", "sltp", "remove"):
            return _error(400, "invalid_request", f"unknown action {action!r}")
        return await _submit(simulator, intent_id, action, flat)

    return app


async def _read(simulator: ReplaySimulator, fn):
    import asyncio

    return await asyncio.to_thread(simulator.read, fn, timeout_s=READ_TIMEOUT_S)


async def _submit(
    simulator: ReplaySimulator, intent_id: str, action: str, body: dict[str, Any], *, strip_closes: bool = False
):
    import asyncio

    try:
        outcome = await asyncio.to_thread(simulator.submit, intent_id, action, body, timeout_s=SUBMIT_TIMEOUT_S)
    except DuplicateIntentError:
        return _error(400, "duplicate_intent", f"intent {intent_id} was already received")
    except SubmitTimeoutError:
        return _error(503, "submit_pending", "outcome not yet visible; resolve by lookup")
    except ValueError as exc:
        return _error(400, "invalid_request", str(exc))
    if strip_closes:
        outcome = {key: value for key, value in outcome.items() if key != "closes"}
    return outcome


__all__ = ["SimulatedBroker", "create_edge_app"]
