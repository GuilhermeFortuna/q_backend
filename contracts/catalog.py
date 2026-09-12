# GENERATED FILE - DO NOT EDIT. Source schemas: schema/catalog/dataset-manifest.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class DatasetManifest:
    arrow_schema: dict[str, Any]
    checksum_algorithm: Literal["sha256", "sha512", "blake3", "md5"]
    dataset_id: str
    files: list[dict[str, Any]]
    published_at: str
    row_count: int
    state: Literal["publishing", "published", "tombstoned", "deleted"]
    subject: dict[str, Any]
    supersedes: str | None
    time_range: dict[str, Any]
    tombstone: dict[str, Any] | None
    version: int
