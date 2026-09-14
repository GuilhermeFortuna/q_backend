# GENERATED FILE - DO NOT EDIT. Source schemas: schema/stream/control/cursor-expired.schema.json, schema/stream/control/epoch-changed.schema.json, schema/stream/control/lagging.schema.json, schema/stream/control/rejected.schema.json, schema/stream/control/subscribe.schema.json, schema/stream/control/subscribed.schema.json, schema/stream/envelope.schema.json, schema/stream/payloads/job-progress.schema.json, schema/stream/payloads/job-terminal.schema.json, schema/stream/replay/history-expired.schema.json, schema/stream/replay/history-page.schema.json, schema/stream/replay/latest.schema.json, schema/stream/replay/watermark.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CursorExpiredFrame:
    topic: str
    type: Literal["cursor_expired"]
    cursor: str | None = None


@dataclass(frozen=True)
class EpochChangedFrame:
    new_epoch: str
    topic: str
    type: Literal["epoch_changed"]
    previous_epoch: str | None = None


@dataclass(frozen=True)
class HistoryExpiredResponse:
    requested_from_seq: int
    topic: str
    oldest_available_seq: int | None = None


@dataclass(frozen=True)
class HistoryPageResponse:
    entries: list[StreamEnvelope]
    epoch: str
    next_seq: int | None
    topic: str


@dataclass(frozen=True)
class JobProgressPayload:
    job_id: str
    kind: Literal[
        "alpha_research",
        "backtest",
        "discovery_ab",
        "encoder_ablation",
        "neural_training",
        "optimization",
        "storage_ingest",
        "strategy_search",
        "walkforward",
    ]
    progress: float | None
    status: Literal["queued", "running"]
    message: str | None = None


@dataclass(frozen=True)
class JobTerminalPayload:
    finished_at: str
    job_id: str
    kind: Literal[
        "alpha_research",
        "backtest",
        "discovery_ab",
        "encoder_ablation",
        "neural_training",
        "optimization",
        "storage_ingest",
        "strategy_search",
        "walkforward",
    ]
    status: Literal["completed", "failed", "cancelled"]
    error: str | None = None


@dataclass(frozen=True)
class LaggingFrame:
    from_seq: int
    topic: str
    type: Literal["lagging"]


@dataclass(frozen=True)
class LatestValuesResponse:
    entries: dict[str, Any]
    topic: str


@dataclass(frozen=True)
class RejectedFrame:
    reason: Literal[
        "unknown_topic",
        "unsupported_schema_major",
        "stream_unavailable",
        "invalid_frame",
    ]
    type: Literal["rejected"]
    detail: str | None = None
    topic: str | None = None


@dataclass(frozen=True)
class SnapshotWatermark:
    pass


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
    key: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscribeFrame:
    topics: list[str]
    cursors: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscribedFrame:
    topics: dict[str, Any]
    type: Literal["subscribed"]
