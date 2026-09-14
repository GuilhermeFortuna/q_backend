from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    message: str
    code: str | None = None
    details: Any | None = None


class HistoryPageResponse(BaseModel):
    topic: str
    epoch: str
    entries: list[dict[str, Any]]
    next_seq: int | None


class HistoryExpiredResponse(BaseModel):
    topic: str
    requested_from_seq: int
    oldest_available_seq: int | None = None


class EpochMismatchResponse(BaseModel):
    topic: str
    requested_epoch: str
    current_epoch: str


class LatestResponse(BaseModel):
    topic: str
    entries: dict[str, dict[str, Any]]


class JobSnapshotItem(BaseModel):
    kind: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: float | None = None
    message: str | None = None
    progress_seq: int | None = None
    progress_epoch: str | None = None


class JobSnapshotResponse(BaseModel):
    jobs: list[JobSnapshotItem]
    watermark: dict[str, dict[str, Any]] = Field(
        ...,
        description="Snapshot watermark keyed by topic name.",
    )
