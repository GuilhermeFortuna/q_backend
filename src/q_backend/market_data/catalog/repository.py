"""Database repository functions for the market-data lake catalog."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from q_contracts.catalog import DatasetManifest
from q_backend.market_data.catalog.partitions import Subject, WrittenFile
from q_backend.storage.db.catalog_models import Dataset, DatasetFile


def to_manifest(dataset: Dataset) -> DatasetManifest:
    """Convert a Dataset ORM model to the vendored DatasetManifest contract dataclass."""
    if dataset.state in ("published", "publishing"):
        if dataset.tombstoned_at is not None or dataset.deletable_after is not None:
            raise ValueError(f"Dataset in state {dataset.state} cannot have tombstone info")
        tombstone = None
    elif dataset.state in ("tombstoned", "deleted"):
        if dataset.tombstoned_at is None or dataset.deletable_after is None:
            raise ValueError(f"Dataset in state {dataset.state} must have tombstoned_at and deletable_after")
        tombstone = {
            "tombstoned_at": dataset.tombstoned_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deletable_after": dataset.deletable_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    else:
        raise ValueError(f"Unknown dataset state: {dataset.state}")

    subject: dict[str, Any] = {
        "kind": dataset.kind,
        "symbol": dataset.symbol,
    }
    if dataset.kind == "bars":
        subject["timeframe"] = dataset.timeframe

    files = [
        {
            "path": f.path,
            "size_bytes": f.size_bytes,
            "checksum": f.checksum,
        }
        for f in dataset.files
    ]

    time_start_iso = dataset.time_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_end_iso = dataset.time_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    published_at_iso = dataset.published_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return DatasetManifest(
        dataset_id=str(dataset.dataset_id),
        subject=subject,
        version=dataset.version,
        supersedes=str(dataset.supersedes) if dataset.supersedes else None,
        state=dataset.state,  # type: ignore[arg-type]
        published_at=published_at_iso,
        checksum_algorithm=dataset.checksum_algorithm,  # type: ignore[arg-type]
        files=files,
        arrow_schema=dataset.arrow_schema,
        row_count=dataset.row_count,
        time_range={"start": time_start_iso, "end": time_end_iso},
        tombstone=tombstone,
    )
