# GENERATED FILE - DO NOT EDIT. Source schemas: schema/stream/control/cursor-expired.schema.json, schema/stream/control/epoch-changed.schema.json, schema/stream/control/lagging.schema.json, schema/stream/control/subscribe.schema.json, schema/stream/control/subscribed.schema.json, schema/stream/envelope.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CursorExpiredFrame:
    topic: str
    cursor: str | None = None


@dataclass(frozen=True)
class EpochChangedFrame:
    new_epoch: str
    topic: str
    previous_epoch: str | None = None


@dataclass(frozen=True)
class LaggingFrame:
    from_seq: int
    topic: str


@dataclass(frozen=True)
class StreamEnvelope:
    epoch: str
    origin_ts: str
    payload: str | dict[str, Any]
    payload_kind: Literal["arrow_ipc", "control"]
    payload_schema: str
    producer_id: str
    schema_major: int
    seq: int
    topic: Literal[
        "bars.completed",
        "bars.forming",
        "decisions",
        "deployments",
        "fills",
        "jobs.progress",
        "jobs.terminal",
        "ledger",
        "orders",
        "quotes",
        "risk",
    ]


@dataclass(frozen=True)
class SubscribeFrame:
    topics: list[str]


@dataclass(frozen=True)
class SubscribedFrame:
    topics: dict[str, Any]
