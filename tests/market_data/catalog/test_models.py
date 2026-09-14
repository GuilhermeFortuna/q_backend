"""Unit tests for catalog database models and manifest mapping (SQLite)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from q_contracts.catalog import DatasetManifest
from q_backend.market_data.catalog.repository import to_manifest
from q_backend.storage.db.base import Base
from q_backend.storage.db.catalog_models import Dataset, DatasetFile


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_dataset(
    *,
    dataset_id: uuid.UUID | None = None,
    kind: str = "bars",
    symbol: str = "PETR4",
    timeframe: str = "D1",
    version: int = 1,
    state: str = "published",
    supersedes: uuid.UUID | None = None,
    published_at: datetime | None = None,
    tombstoned_at: datetime | None = None,
    deletable_after: datetime | None = None,
) -> Dataset:
    now = datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    return Dataset(
        dataset_id=dataset_id or uuid.uuid4(),
        kind=kind,
        symbol=symbol,
        timeframe=timeframe,
        version=version,
        state=state,
        supersedes=supersedes,
        published_at=published_at or now,
        checksum_algorithm="sha256",
        arrow_schema={
            "name": "bars",
            "fields": [
                {"name": "time", "type": "timestamp[us]", "nullable": False, "tz": "naive-wallclock-America/Sao_Paulo"},
                {"name": "open", "type": "float64", "nullable": False},
                {"name": "high", "type": "float64", "nullable": False},
                {"name": "low", "type": "float64", "nullable": False},
                {"name": "close", "type": "float64", "nullable": False},
                {"name": "tick_volume", "type": "int64", "nullable": False},
                {"name": "spread", "type": "int64", "nullable": False},
                {"name": "real_volume", "type": "int64", "nullable": False},
            ],
        },
        row_count=100,
        time_start=now,
        time_end=now + timedelta(days=10),
        tombstoned_at=tombstoned_at,
        deletable_after=deletable_after,
    )


def test_two_published_rows_for_same_subject_raises_integrity_error(session: Session) -> None:
    d1 = _make_dataset(version=1, state="published")
    d2 = _make_dataset(version=2, state="published")
    session.add(d1)
    session.commit()

    session.add(d2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_invalid_state_raises_integrity_error(session: Session) -> None:
    d = _make_dataset(state="archived")
    session.add(d)
    with pytest.raises(IntegrityError):
        session.commit()


def test_to_manifest_on_tombstoned_row_without_deletable_after_raises() -> None:
    now = datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    d = _make_dataset(
        state="tombstoned",
        tombstoned_at=now,
        deletable_after=None,
    )
    with pytest.raises(ValueError, match="deletable_after"):
        to_manifest(d)


def test_to_manifest_valid_published() -> None:
    d = _make_dataset(state="published")
    file = DatasetFile(
        dataset_id=d.dataset_id,
        path="ohlcv/PETR4/D1/2025.parquet",
        ordinal=0,
        size_bytes=1024,
        checksum="a" * 64,
        partition_start=d.time_start,
        partition_end=d.time_end,
    )
    d.files.append(file)
    manifest = to_manifest(d)
    assert isinstance(manifest, DatasetManifest)
    assert manifest.dataset_id == str(d.dataset_id)
    assert manifest.state == "published"
    assert manifest.tombstone is None
    assert manifest.version == 1
    assert len(manifest.files) == 1
    assert manifest.files[0]["path"] == "ohlcv/PETR4/D1/2025.parquet"
    assert manifest.subject == {"kind": "bars", "symbol": "PETR4", "timeframe": "D1"}


def test_to_manifest_valid_tombstoned() -> None:
    now = datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    d = _make_dataset(
        state="tombstoned",
        tombstoned_at=now,
        deletable_after=now + timedelta(days=7),
    )
    manifest = to_manifest(d)
    assert manifest.state == "tombstoned"
    assert manifest.tombstone is not None
    assert manifest.tombstone["tombstoned_at"] == "2025-01-02T12:00:00Z"
    assert manifest.tombstone["deletable_after"] == "2025-01-09T12:00:00Z"
