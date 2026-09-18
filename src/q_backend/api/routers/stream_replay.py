"""REST replay endpoints for stream history, latest values, and job snapshots."""

from __future__ import annotations

import redis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.schemas.stream import (
    EpochMismatchResponse,
    ErrorResponse,
    ExecutionSnapshotResponse,
    HistoryExpiredResponse,
    HistoryPageResponse,
    JobSnapshotResponse,
    LatestResponse,
)
from q_backend.storage.db import engine as db_engine
from q_backend.storage.redis.client import get_redis
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.outbox import OutboxTopicError
from q_backend.streaming.snapshot import (
    EpochMismatch,
    HistoryExpired,
    StreamUnavailable,
    history_to_response,
    job_snapshot_to_response,
    latest_to_response,
    read_execution_snapshot,
    read_history,
    read_job_snapshot,
    read_latest,
)

router = APIRouter(tags=["stream"])


def get_stream_redis() -> redis.Redis:
    return get_redis()


def get_latest_redis() -> redis.Redis:
    return get_binary_redis()


@router.get(
    "/api/v1/stream/jobs/snapshot",
    response_model=JobSnapshotResponse,
    responses={503: {"model": ErrorResponse}},
)
def get_job_snapshot(
    client: redis.Redis = Depends(get_stream_redis),
) -> JobSnapshotResponse | JSONResponse:
    try:
        client.ping()
    except Exception:  # noqa: BLE001 - best-effort Redis availability check
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(message="Stream unavailable", code="stream_unavailable").model_dump(),
        )

    result = read_job_snapshot(db_engine.create_session_factory(), client)
    return JobSnapshotResponse.model_validate(job_snapshot_to_response(result))


@router.get(
    "/api/v1/stream/execution/snapshot",
    response_model=ExecutionSnapshotResponse,
    responses={503: {"model": ErrorResponse}},
)
def get_execution_snapshot(
    deployments_limit: int = Query(50, ge=1, le=500),
    decisions_limit: int = Query(500, ge=1, le=2000),
    orders_limit: int = Query(500, ge=1, le=2000),
    fills_limit: int = Query(500, ge=1, le=2000),
    risk_limit: int = Query(500, ge=1, le=2000),
    ledger_limit: int = Query(500, ge=1, le=2000),
) -> ExecutionSnapshotResponse | JSONResponse:
    try:
        factory = db_engine.create_session_factory()
        result = read_execution_snapshot(
            factory,
            deployments_limit=deployments_limit,
            decisions_limit=decisions_limit,
            orders_limit=orders_limit,
            fills_limit=fills_limit,
            risk_limit=risk_limit,
            ledger_limit=ledger_limit,
        )
        return ExecutionSnapshotResponse.model_validate(result)
    except Exception:  # noqa: BLE001 - map database connection / query failure to 503
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(message="Database unavailable", code="database_unavailable").model_dump(),
        )


@router.get(
    "/api/v1/stream/{topic}/history",
    response_model=HistoryPageResponse,
    responses={
        400: {"model": ErrorResponse},
        409: {"model": EpochMismatchResponse},
        410: {"model": HistoryExpiredResponse},
    },
)
def get_history(
    topic: str,
    epoch: str = Query(...),
    from_seq: int = Query(..., ge=1, alias="from_seq"),
    limit: int = Query(500, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> HistoryPageResponse | JSONResponse:
    try:
        result = read_history(session, topic, epoch, from_seq, limit)
    except OutboxTopicError as exc:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(message=str(exc), code="invalid_topic").model_dump(),
        )
    except EpochMismatch as exc:
        return JSONResponse(
            status_code=409,
            content=EpochMismatchResponse(
                topic=exc.topic,
                requested_epoch=exc.requested_epoch,
                current_epoch=exc.current_epoch,
            ).model_dump(),
        )
    except HistoryExpired as exc:
        return JSONResponse(
            status_code=410,
            content=HistoryExpiredResponse(
                topic=exc.topic,
                requested_from_seq=exc.requested_from_seq,
                oldest_available_seq=exc.oldest_available_seq,
            ).model_dump(),
        )

    return HistoryPageResponse.model_validate(history_to_response(result))


@router.get(
    "/api/v1/stream/{topic}/latest",
    response_model=LatestResponse,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_latest(
    topic: str,
    key: str | None = None,
    client: redis.Redis = Depends(get_latest_redis),
) -> LatestResponse | JSONResponse:
    try:
        entries = read_latest(client, topic, key=key)
    except OutboxTopicError as exc:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(message=str(exc), code="invalid_topic").model_dump(),
        )
    except StreamUnavailable:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(message="Stream unavailable", code="stream_unavailable").model_dump(),
        )

    return LatestResponse.model_validate(latest_to_response(topic, entries))
