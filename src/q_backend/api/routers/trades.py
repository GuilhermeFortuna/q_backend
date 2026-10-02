"""Session trade snapshot and paged history (Q-080).

The API holds no ingestion state. A snapshot is requested from the market publisher
through Redis; pages are read from the immutable files the publisher shares.
"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache

import redis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from q_backend.api.schemas.stream import ErrorResponse
from q_backend.api.schemas.trades import (
    HISTORY_HEADER_NAMES,
    TradeHistoryPageHeaders,
    TradeHistoryPending,
    TradeSnapshotResponse,
    TradeSourceStatus,
    TradeWatermark,
)
from q_backend.storage.settings import get_settings
from q_backend.streaming.market.trade_history import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    PublisherUnavailable,
    SnapshotExpired,
    SnapshotNotFound,
    TradeHistoryReader,
    TradeSnapshotClient,
    cache_from_settings,
    public_descriptor,
    valid_symbol,
)
from q_backend.streaming.redis_binary import get_binary_redis

router = APIRouter(tags=["market"])

ARROW_STREAM = "application/vnd.apache.arrow.stream"


@lru_cache
def _redis() -> redis.Redis:
    return get_binary_redis()


def get_trade_client() -> TradeSnapshotClient:
    return TradeSnapshotClient(_redis(), timeout_s=get_settings().trade_request_timeout_s)


def get_trade_reader() -> TradeHistoryReader:
    return TradeHistoryReader(cache_from_settings(get_settings()), _redis())


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(message=message, code=code).model_dump())


@router.get(
    "/api/v1/market/trades/snapshot",
    response_model=TradeSnapshotResponse,
    responses={
        202: {"model": TradeHistoryPending},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def get_trade_session_snapshot(
    symbol: str = Query(...),
    client: TradeSnapshotClient = Depends(get_trade_client),
):
    """Freeze the session prefix and live watermark with the publisher and issue a token."""
    if not valid_symbol(symbol):
        return _error(404, "unknown_symbol", f"Symbol {symbol!r} is not published.")
    try:
        outcome = client.request_snapshot(symbol)
    except PublisherUnavailable as exc:
        return _error(503, "trade_source_unavailable", str(exc))
    if outcome.kind == "ready":
        return TradeSnapshotResponse(**_snapshot_fields(public_descriptor(outcome.descriptor)))
    if outcome.kind == "pending":
        pending = outcome.pending or {}
        headers = {"Retry-After": "1"}
        if pending.get("covered_to"):
            headers["X-Q-Trade-Backfill-Covered-To"] = str(pending["covered_to"])
        headers["X-Q-Trade-Backfill-Rows"] = str(pending.get("rows", 0))
        body = TradeHistoryPending(status="backfill_pending", status_token=pending["status_token"])
        return JSONResponse(status_code=202, content=asdict(body), headers=headers)
    if outcome.kind == "unknown_symbol":
        return _error(404, "unknown_symbol", outcome.message or f"Symbol {symbol!r} is not published.")
    code = "trade_cache_resource_limit" if outcome.kind == "resource_limit" else "trade_source_unavailable"
    return _error(503, code, outcome.message or "The trade source is unavailable for this symbol.")


def _snapshot_fields(descriptor: dict) -> dict:
    fields = dict(descriptor)
    fields["frozen_watermark"] = TradeWatermark(**fields["frozen_watermark"])
    coverage = dict(fields["coverage"])
    coverage["last_trade_watermark"] = TradeWatermark(**coverage["last_trade_watermark"])
    fields["coverage"] = TradeSourceStatus(**coverage)
    return fields


@router.get(
    "/api/v1/market/trades/history",
    responses={
        200: {"content": {ARROW_STREAM: {}}},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def get_trade_session_history(
    snapshot_id: str = Query(...),
    cursor: str | None = Query(None),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    reader: TradeHistoryReader = Depends(get_trade_reader),
):
    """One immutable Arrow IPC page of a snapshot, with its watermark in the headers."""
    try:
        descriptor, data, next_cursor, page_count = reader.read(snapshot_id, cursor, limit)
    except SnapshotNotFound:
        return _error(404, "snapshot_not_found", "The trade snapshot was not found.")
    except SnapshotExpired as exc:
        message = {
            "snapshot_expired": "The immutable trade snapshot token expired; request a new snapshot.",
            "source_generation_replaced": "The trade source generation was replaced; request a new snapshot.",
            "publisher_restarted": "The market publisher restarted; request a new snapshot.",
        }.get(exc.reason, "The trade snapshot is no longer available; request a new snapshot.")
        return _error(410, "snapshot_expired", message)
    except InvalidCursor as exc:
        return _error(400, "invalid_cursor", str(exc))
    except PublisherUnavailable as exc:
        return _error(503, "trade_source_unavailable", str(exc))
    watermark = descriptor["frozen_watermark"]
    meta = TradeHistoryPageHeaders(
        snapshot_id=descriptor["snapshot_id"],
        source_generation=descriptor["source_generation"],
        symbol=descriptor["symbol"],
        volume_field=descriptor["volume_field"],
        volume_unit=descriptor["volume_unit"],
        page_count=page_count,
        frozen_epoch=watermark["epoch"],
        frozen_seq=watermark["seq"],
        next_cursor=next_cursor,
    )
    headers = {HISTORY_HEADER_NAMES[name]: "" if value is None else str(value) for name, value in asdict(meta).items()}
    return Response(content=data, media_type=ARROW_STREAM, headers=headers)
