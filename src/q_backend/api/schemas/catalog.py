"""Pydantic schemas for the lake dataset catalog API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ManifestFile(BaseModel):
    path: str
    size_bytes: int
    checksum: str


class DatasetManifestResponse(BaseModel):
    dataset_id: str
    subject: dict[str, Any]
    version: int
    supersedes: str | None = None
    state: Literal["publishing", "published", "tombstoned", "deleted"]
    published_at: str
    checksum_algorithm: Literal["sha256", "sha512", "blake3", "md5"]
    files: list[ManifestFile]
    arrow_schema: dict[str, Any]
    row_count: int
    time_range: dict[str, Any]
    tombstone: dict[str, Any] | None = None


class DatasetListResponse(BaseModel):
    root: str
    datasets: list[DatasetManifestResponse]
