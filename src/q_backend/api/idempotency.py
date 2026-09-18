"""Stored-result idempotency for mutating execution commands."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Callable, TypeVar

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from q_backend.storage.db.base import utc_now
from q_backend.storage.db.idempotency_models import CommandIdempotency
from q_backend.storage.db.engine import create_session_factory
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)
T = TypeVar("T")
IDEMPOTENT_OPERATION_NAMES = {
    "create_execution_account",
    "create_execution_deployment",
    "deployment_action",
    "resolve_execution_order",
    "update_kill_switch",
}


def canonical_body_hash(body: Any) -> str:
    """Hash a request body using stable JSON serialization."""

    if body is None:
        body = {}
    if isinstance(body, bytes):
        body = json.loads(body or b"{}")
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _error_body(exc: HTTPException) -> dict[str, Any]:
    return {"detail": jsonable_encoder(exc.detail)}


def _idempotency_error(message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=400 if code == "idempotency_key_required" else 409,
        content={"message": message, "code": code},
        headers={"Retry-After": "1"} if code == "idempotency_in_progress" else None,
    )


class IdempotentCommand:
    """Claims and records one execution command in its surrounding transaction."""

    def __init__(
        self,
        *,
        key: uuid.UUID | None,
        method: str,
        path: str,
        body: Any,
        enforced: bool | None = None,
    ) -> None:
        self.key = key
        self.method = method.upper()
        self.path = path
        self.body_sha256 = canonical_body_hash(body)
        self.enforced = get_settings().execution_idempotency_enforced if enforced is None else enforced
        self.claimed = False
        self.early_response: Response | None = None

    @classmethod
    def from_request(cls, request: Request, key: uuid.UUID | None, body: Any) -> "IdempotentCommand":
        return cls(key=key, method=request.method, path=request.url.path, body=body)

    def replay_or_claim(self, session: Session) -> Response | None:
        """Return an early response, or claim the key for the current command."""

        if self.key is None:
            if self.enforced:
                return _idempotency_error(
                    "Idempotency-Key header is required for this command",
                    "idempotency_key_required",
                )
            logger.warning(
                "execution command accepted without Idempotency-Key: %s %s",
                self.method,
                self.path,
            )
            return None

        dialect = session.get_bind().dialect.name
        values = {
            "key": self.key,
            "method": self.method,
            "path": self.path,
            "body_sha256": self.body_sha256,
            "status_code": 0,
            "response_body": {},
        }
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

            statement = (
                dialect_insert(CommandIdempotency)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[CommandIdempotency.key])
            )
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert

            statement = (
                dialect_insert(CommandIdempotency)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[CommandIdempotency.key])
            )
        else:  # pragma: no cover - PostgreSQL and SQLite cover production/tests.
            statement = insert(CommandIdempotency).values(**values)

        result = session.execute(statement)
        session.flush()
        if result.rowcount == 1:
            self.claimed = True
            return None

        existing = session.execute(select(CommandIdempotency).where(CommandIdempotency.key == self.key)).scalar_one()
        if existing.method != self.method or existing.path != self.path or existing.body_sha256 != self.body_sha256:
            return _idempotency_error(
                "Idempotency-Key was already used for a different command",
                "idempotency_key_reused",
            )
        if existing.status_code == 0:
            return _idempotency_error(
                "The command for this Idempotency-Key is still in progress",
                "idempotency_in_progress",
            )
        return JSONResponse(
            status_code=existing.status_code,
            content=existing.response_body,
            headers={"Idempotency-Replayed": "true"},
        )

    def prepare(self, session: Session) -> None:
        """Claim before FastAPI validates the request body."""

        self.early_response = self.replay_or_claim(session)

    def store(self, session: Session, status_code: int, body: Any) -> None:
        """Store the exact JSON response before the command transaction commits."""

        if self.claimed:
            session.execute(
                update(CommandIdempotency)
                .where(CommandIdempotency.key == self.key)
                .values(status_code=status_code, response_body=jsonable_encoder(body))
            )
            session.flush()

    def execute(
        self,
        session: Session,
        operation: Callable[[], T],
        *,
        status_code: int = 200,
    ) -> T | Response:
        """Run a command, recording terminal client/business errors as results."""

        early_response = self.early_response
        if early_response is None and not self.claimed:
            early_response = self.replay_or_claim(session)
        if early_response is not None:
            return early_response

        try:
            result = operation()
        except HTTPException as exc:
            if exc.status_code >= 500:
                session.rollback()
            else:
                self.store(session, exc.status_code, _error_body(exc))
            return JSONResponse(status_code=exc.status_code, content=_error_body(exc))

        self.store(session, status_code, result)
        return result


def prune_idempotency(session: Session, *, older_than: timedelta = timedelta(hours=24)) -> int:
    """Delete stored command results older than the idempotency retention window."""

    cutoff = utc_now() - older_than
    result = session.execute(delete(CommandIdempotency).where(CommandIdempotency.created_at < cutoff))
    session.flush()
    return int(result.rowcount or 0)


class IdempotentRoute(APIRoute):
    """Claim covered commands before FastAPI parses their typed request body."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.name not in IDEMPOTENT_OPERATION_NAMES:
            return handler

        async def wrapped(request: Request) -> Response:
            from q_backend.api.deps import get_session

            override = getattr(request.scope.get("app"), "dependency_overrides", {}).get(get_session)
            lifecycle = None
            if override is None:
                session = create_session_factory()()
            else:
                candidate = override()
                if inspect.isgenerator(candidate):
                    lifecycle = candidate
                    session = next(candidate)
                else:  # pragma: no cover - FastAPI session overrides are normally generators.
                    session = candidate
            request.state.idempotency_session = session
            raw_body = await request.body()
            try:
                body = json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                body = {"_raw": raw_body.decode("utf-8", errors="replace")}
            raw_key = request.headers.get("Idempotency-Key")
            try:
                key = uuid.UUID(raw_key) if raw_key is not None else None
            except ValueError:
                key = None
            command = IdempotentCommand.from_request(request, key, body)
            command.prepare(session)
            request.state.idempotency_command = command
            try:
                response = await handler(request)
                if command.claimed and response.status_code == 422:
                    response_body = json.loads(response.body or b"{}")
                    command.store(session, 422, response_body)
                if response.status_code >= 500:
                    session.rollback()
                else:
                    session.commit()
                return response
            except RequestValidationError as exc:
                if command.early_response is not None:
                    session.commit()
                    return command.early_response
                body = {"detail": jsonable_encoder(exc.errors())}
                command.store(session, 422, body)
                session.commit()
                return JSONResponse(status_code=422, content=body)
            except Exception:
                session.rollback()
                raise
            finally:
                if lifecycle is not None:
                    lifecycle.close()
                session.close()

        return wrapped
