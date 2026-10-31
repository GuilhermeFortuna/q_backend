"""Administrative listener of the replay simulator: ``/replay/v1``, the external controller surface.

Only the replay controller uses this listener. No live gateway, execution edge or control API
exposes these routes, and the trading stack never calls them. Requests carry an optional shared
token; the listener is meant to bind loopback only (see ``replay.server``).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.controller import ACTIONS, ReplayNotFoundError
from q_backend.replay.dataset import DatasetError
from q_backend.replay.journal import RunAlreadyExistsError
from q_backend.replay.service import ReplaySimulator


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    action: str
    rate: float | None = None
    market_ms: int | None = Field(default=None, ge=1)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "code": code})


def create_admin_app(simulator: ReplaySimulator, *, token: str | None = None, on_created=None) -> FastAPI:
    """Build the admin app. ``on_created`` runs after a run is prepared (starts run listeners)."""
    app = FastAPI(title="Q replay administrative API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if token is not None:
            supplied = request.headers.get("X-Replay-Token", "")
            if not secrets.compare_digest(supplied, token):
                return _error(401, "unauthorized", "missing or invalid replay token")
        return await call_next(request)

    @app.post("/replay/v1/runs", status_code=201)
    async def create_run(request: Request):
        try:
            config = ReplayRunConfig.model_validate(await request.json())
        except ValidationError as exc:
            return _error(422, "invalid_config", str(exc))
        run_id = str(uuid.uuid4())
        try:
            status = await asyncio.to_thread(simulator.create, config, run_id=run_id, generation=f"gen-{run_id[:8]}")
        except RunAlreadyExistsError as exc:
            return _error(409, "run_exists", str(exc))
        except DatasetError as exc:
            return _error(422, "invalid_dataset", str(exc))
        if on_created is not None:
            await asyncio.to_thread(on_created)
        return _status_payload(status)

    @app.get("/replay/v1/runs/current")
    async def current():
        try:
            return _status_payload(await asyncio.to_thread(simulator.status))
        except ReplayNotFoundError:
            return _error(404, "not_found", "no replay run exists")
        except LookupError:
            return _error(404, "not_found", "no replay run exists")

    @app.post("/replay/v1/runs/current/commands")
    async def command(request: Request):
        try:
            body = CommandBody.model_validate(await request.json())
        except ValidationError as exc:
            return _error(422, "invalid_command", str(exc))
        if body.action not in ACTIONS:
            return _error(422, "invalid_command", f"unknown action {body.action!r}")
        try:
            result = await asyncio.to_thread(
                simulator.command,
                command_id=str(body.command_id),
                expected_revision=body.expected_revision,
                action=body.action,
                rate=body.rate,
                market_ms=body.market_ms,
            )
        except ValueError as exc:
            return _error(422, "invalid_command", str(exc))
        except LookupError:
            return _error(404, "not_found", "no replay run exists")
        return {
            "command_id": result.command_id,
            "accepted": result.accepted,
            "reason": result.reason,
            "revision": result.revision,
            "state": result.state,
        }

    @app.get("/replay/v1/runs/current/journal")
    async def journal():
        try:
            entries = await asyncio.to_thread(simulator.journal)
        except LookupError:
            return _error(404, "not_found", "no replay run exists")
        return [
            {"seq": seq, "kind": kind, "market_time_us": market_us, "payload_hash": digest, "payload": _json(payload)}
            for seq, kind, market_us, digest, payload in entries
        ]

    return app


def _status_payload(status) -> dict[str, Any]:
    return {
        "run_id": status.run_id,
        "generation": status.generation,
        "state": status.state,
        "revision": status.revision,
        "rate": status.rate,
        "canonical_cursor_utc": _utc(status.canonical_cursor_us),
        "visible_cursor_utc": _utc(status.visible_cursor_us),
        "queue_depth": status.queue_depth,
        "lag_ms": 0,
        "outstanding_intents": 0,
        "config_hash": status.config_hash,
        "dataset_checksums": list(status.dataset_checksums),
        "last_error": status.last_error,
    }


def _utc(market_us: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(market_us / 1e6, tz=UTC).isoformat().replace("+00:00", "Z")


def _json(payload: str) -> Any:
    import json

    return json.loads(payload)
