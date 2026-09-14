"""Unit and integration tests for lake catalog adoption."""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, to_manifest
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base
from q_backend.storage.db.catalog_models import Dataset


@pytest.fixture
def manifest_schema() -> dict:
    schema_path = Path(__file__).resolve().parents[3] / "contracts/schema/catalog/dataset-manifest.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


@pytest.fixture
def lake_copy(tmp_path: Path) -> Path:
    src_fixture = Path(__file__).resolve().parents[2] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)
    return dst_lake


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_adoption_over_fixture_copy(lake_copy: Path, session_factory, manifest_schema: dict) -> None:
    # Record stats before adoption
    before_stats = {
        p.relative_to(lake_copy).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in lake_copy.rglob("*.parquet")
    }
    assert len(before_stats) == 5  # 2 PETR4 bars + 1 WIN$N bars + 2 WIN$N ticks

    fixed_now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    catalog = LakeCatalog(
        session_factory=session_factory,
        root=lake_copy,
        clock=lambda: fixed_now,
    )

    report = catalog.adopt()
    assert len(report.adopted) == 3
    assert len(report.skipped_existing) == 0
    assert report.files == 5

    # Check that bytes and mtime_ns are completely unchanged
    after_stats = {
        p.relative_to(lake_copy).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in lake_copy.rglob("*.parquet")
    }
    assert after_stats == before_stats

    # Check each adopted dataset
    with session_factory() as session:
        petr4_d1 = current_dataset(session, Subject(kind="bars", symbol="PETR4", timeframe="D1"))
        assert petr4_d1 is not None
        assert petr4_d1.version == 1
        assert petr4_d1.state == "published"
        manifest_petr4 = to_manifest(petr4_d1)
        jsonschema.validate(instance=dataclasses.asdict(manifest_petr4), schema=manifest_schema)

        winn_m15 = current_dataset(session, Subject(kind="bars", symbol="WIN$N", timeframe="M15"))
        assert winn_m15 is not None
        assert winn_m15.version == 1
        manifest_winn = to_manifest(winn_m15)
        jsonschema.validate(instance=dataclasses.asdict(manifest_winn), schema=manifest_schema)
        # 09:00 naive Brasília bar must serialize as 12:00 UTC
        assert manifest_winn.time_range["start"].endswith("T12:00:00Z")

        winn_ticks = current_dataset(session, Subject(kind="ticks", symbol="WIN$N", timeframe=""))
        assert winn_ticks is not None
        assert winn_ticks.version == 1
        manifest_ticks = to_manifest(winn_ticks)
        jsonschema.validate(instance=dataclasses.asdict(manifest_ticks), schema=manifest_schema)

        all_datasets = session.query(Dataset).all()
        assert len(all_datasets) == 3

    # Run adopt a second time: must report skipped_existing and change nothing
    second_report = catalog.adopt()
    assert len(second_report.adopted) == 0
    assert len(second_report.skipped_existing) == 3

    with session_factory() as session:
        assert session.query(Dataset).count() == 3
