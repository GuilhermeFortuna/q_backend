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
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.storage.db.catalog_models import Dataset, DatasetFile


def current_dataset(session: Session, subject: Subject) -> Dataset | None:
    """Return the currently published dataset for a subject, or None."""
    stmt = sa.select(Dataset).where(
        Dataset.kind == subject.kind,
        Dataset.symbol == subject.symbol,
        Dataset.timeframe == subject.timeframe,
        Dataset.state == "published",
    )
    return session.scalars(stmt).one_or_none()


def get_dataset(session: Session, dataset_id: uuid.UUID) -> Dataset | None:
    """Return dataset by UUID, regardless of state."""
    return session.get(Dataset, dataset_id)


def list_current_datasets(
    session: Session,
    *,
    kind: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[Dataset]:
    """List all currently published datasets, optionally filtered."""
    stmt = sa.select(Dataset).where(Dataset.state == "published")
    if kind:
        stmt = stmt.where(Dataset.kind == kind)
    if symbol:
        stmt = stmt.where(Dataset.symbol == symbol)
    if timeframe is not None:
        stmt = stmt.where(Dataset.timeframe == timeframe)
    stmt = stmt.order_by(Dataset.symbol, Dataset.kind, Dataset.timeframe)
    return list(session.scalars(stmt).all())


def lock_subject(session: Session, subject: Subject) -> None:
    """Acquire transaction-scoped advisory lock on Postgres for the subject; no-op on SQLite."""
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        key_str = f"{subject.kind}:{subject.symbol}:{subject.timeframe}"
        h = hashlib.sha256(key_str.encode("utf-8")).digest()
        lock_id = int.from_bytes(h[:8], byteorder="big", signed=True)
        session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})


def publish_version(
    session: Session,
    subject: Subject,
    files: Sequence[WrittenFile],
    *,
    now: datetime,
    grace: timedelta,
) -> Dataset:
    """Publish a new dataset version for subject, tombstoning any superseded version."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    current = current_dataset(session, subject)
    if current is None:
        new_version = 1
        supersedes_id = None
    else:
        new_version = current.version + 1
        supersedes_id = current.dataset_id
        current.state = "tombstoned"
        current.tombstoned_at = now
        current.deletable_after = now + grace

    if not files:
        raise ValueError(f"Cannot publish empty dataset for subject {subject}")

    total_rows = sum(f.rows for f in files)
    min_start_naive = min(f.partition_start for f in files)
    max_end_naive = max(f.partition_end for f in files)

    time_start_utc = min_start_naive.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
    time_end_utc = max_end_naive.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
    arrow_schema = files[0].arrow_schema

    dataset = Dataset(
        dataset_id=uuid.uuid4(),
        kind=subject.kind,
        symbol=subject.symbol,
        timeframe=subject.timeframe,
        version=new_version,
        supersedes=supersedes_id,
        state="published",
        published_at=now,
        checksum_algorithm="sha256",
        arrow_schema=arrow_schema,
        row_count=total_rows,
        time_start=time_start_utc,
        time_end=time_end_utc,
        tombstoned_at=None,
        deletable_after=None,
    )

    for i, f in enumerate(files):
        p_start_utc = f.partition_start.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        p_end_utc = f.partition_end.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        dataset_file = DatasetFile(
            dataset_id=dataset.dataset_id,
            path=f.path,
            ordinal=i,
            size_bytes=f.size_bytes,
            checksum=f.checksum,
            partition_start=p_start_utc,
            partition_end=p_end_utc,
        )
        dataset.files.append(dataset_file)

    session.add(dataset)
    session.flush()
    return dataset


def tombstone_current(
    session: Session,
    subject: Subject,
    *,
    now: datetime,
    grace: timedelta,
) -> Dataset | None:
    """Mark the current published dataset for subject as tombstoned with deadline."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    current = current_dataset(session, subject)
    if current is None:
        return None

    current.state = "tombstoned"
    current.tombstoned_at = now
    current.deletable_after = now + grace
    session.flush()
    return current


def sweepable(session: Session, *, now: datetime) -> list[Dataset]:
    """List tombstoned datasets whose grace period has expired."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stmt = sa.select(Dataset).where(
        Dataset.state == "tombstoned",
        Dataset.deletable_after <= now,
    )
    return list(session.scalars(stmt).all())


def live_paths(session: Session, *, now: datetime | None = None) -> set[str]:
    """Return set of relative file paths listed by published or still-in-grace datasets."""
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        stmt = (
            sa.select(DatasetFile.path)
            .join(Dataset)
            .where(
                sa.or_(
                    Dataset.state == "published",
                    sa.and_(Dataset.state == "tombstoned", Dataset.deletable_after > now),
                )
            )
        )
    else:
        stmt = sa.select(DatasetFile.path).join(Dataset).where(Dataset.state != "deleted")
    return set(session.scalars(stmt).all())


def _format_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
            "tombstoned_at": _format_utc_iso(dataset.tombstoned_at),
            "deletable_after": _format_utc_iso(dataset.deletable_after),
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

    time_start_iso = _format_utc_iso(dataset.time_start)
    time_end_iso = _format_utc_iso(dataset.time_end)
    published_at_iso = _format_utc_iso(dataset.published_at)

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
