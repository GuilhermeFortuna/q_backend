# GENERATED FILE - DO NOT EDIT. Source schemas: schema/edge/common/error.schema.json, schema/edge/common/health.schema.json, schema/edge/execution/check-request.schema.json, schema/edge/execution/check-response.schema.json, schema/edge/execution/deal.schema.json, schema/edge/execution/deals-request.schema.json, schema/edge/execution/deals-response.schema.json, schema/edge/execution/lookup-outcome.schema.json, schema/edge/execution/lookup-request.schema.json, schema/edge/execution/order.schema.json, schema/edge/execution/position.schema.json, schema/edge/execution/positions-request.schema.json, schema/edge/execution/positions-response.schema.json, schema/edge/execution/quote-request.schema.json, schema/edge/execution/quote-response.schema.json, schema/edge/execution/submit-outcome.schema.json, schema/edge/execution/submit-request.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CheckRequest:
    intent_id: str
    order: ExecutionOrder


@dataclass(frozen=True)
class CheckResponse:
    allowed: bool
    margin: float
    retcode: int
    reason: str | None = None


@dataclass(frozen=True)
class DealsRequest:
    window_end: str | int
    window_start: str | int
    magic: int | None = None
    symbol: str | None = None


DealsResponse = list["ExecutionDeal"]


@dataclass(frozen=True)
class EdgeErrorResponse:
    code: Literal['invalid_timeframe', 'symbol_not_found', 'range_unavailable', 'tick_range_too_large', 'invalid_flags', 'mt5_unavailable', 'unauthorized', 'not_found', 'internal_error', 'duplicate_intent', 'schema_major_mismatch']
    error: str


@dataclass(frozen=True)
class EdgeHealthResponse:
    mt5_connected: bool
    schema_version: str
    status: str
    terminal_build: int | None


@dataclass(frozen=True)
class ExecutionDeal:
    order_ticket: int
    price: float
    symbol: str
    ticket: int
    volume: float
    comment: str | None = None
    commission: float | None = None
    entry: int | None = None
    fee: float | None = None
    magic: int | None = None
    profit: float | None = None
    swap: float | None = None
    time_msc: int | None = None
    type: int | None = None


@dataclass(frozen=True)
class ExecutionOrder:
    symbol: str
    volume: float
    comment: str | None = None
    deviation: int | None = None
    magic: int | None = None
    price: float | None = None
    side: Literal['buy', 'sell'] | None = None
    sl: float | None = None
    tp: float | None = None
    type: int | None = None
    type_filling: int | None = None
    type_time: int | None = None


@dataclass(frozen=True)
class ExecutionPosition:
    price_open: float
    symbol: str
    ticket: int
    type: int
    volume: float
    comment: str | None = None
    magic: int | None = None
    price_current: float | None = None
    profit: float | None = None
    sl: float | None = None
    time: int | None = None
    tp: float | None = None


LookupOutcome = dict[str, Any] | dict[str, Any] | dict[str, Any] | dict[str, Any]


@dataclass(frozen=True)
class LookupRequest:
    intent_id: str
    window_end: str | int
    window_start: str | int
    magic: int | None = None


@dataclass(frozen=True)
class PositionsRequest:
    symbol: str | None = None


PositionsResponse = list["ExecutionPosition"]


@dataclass(frozen=True)
class QuoteRequest:
    symbol: str


@dataclass(frozen=True)
class QuoteResponse:
    age_ms: int
    ask: float
    bid: float
    last: float
    symbol: str
    time_msc: int


SubmitOutcome = dict[str, Any] | dict[str, Any] | dict[str, Any]


@dataclass(frozen=True)
class SubmitRequest:
    intent_id: str
    order: ExecutionOrder
