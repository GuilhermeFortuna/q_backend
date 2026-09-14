"""Tests for the lake dataset catalog API (step 11)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.main import app
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/schema/catalog/dataset-manifest.schema.json"


@pytest.fixture
def catalog_api_lake(tmp_path: Path, monkeypatch) -> tuple[Path, sessionmaker, LakeCatalog]:
    src_fixture = Path(__file__).resolve().parents[1] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)

    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(dst_lake))

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)

    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()

    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)
    return dst_lake, sm, catalog


def test_list_datasets_returns_root_and_valid_manifests(catalog_api_lake):
    dst_lake, _, _ = catalog_api_lake
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    client = TestClient(app)
    response = client.get("/api/v1/catalog/datasets")
    assert response.status_code == 200

    data = response.json()
    assert data["root"] == str(dst_lake.resolve())
    datasets = data["datasets"]
    assert len(datasets) == 3

    for manifest in datasets:
        jsonschema.validate(instance=manifest, schema=schema)

    # Test filtering by kind/symbol/timeframe
    resp_filtered = client.get("/api/v1/catalog/datasets?symbol=PETR4&timeframe=D1")
    assert resp_filtered.status_code == 200
    filtered_data = resp_filtered.json()["datasets"]
    assert len(filtered_data) == 1
    assert filtered_data[0]["subject"]["symbol"] == "PETR4"


def test_get_dataset_by_id_tombstoned(catalog_api_lake):
    _, sm, catalog = catalog_api_lake
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # Find PETR4 D1 dataset_id
    with sm() as session:
        ds = current_dataset(session, Subject(kind="bars", symbol="PETR4", timeframe="D1"))
        assert ds is not None
        target_id = str(ds.dataset_id)

    # Tombstone it
    catalog.delete_subject(Subject(kind="bars", symbol="PETR4", timeframe="D1"))

    client = TestClient(app)
    response = client.get(f"/api/v1/catalog/datasets/{target_id}")
    assert response.status_code == 200

    manifest = response.json()
    assert manifest["dataset_id"] == target_id
    assert manifest["state"] == "tombstoned"
    assert manifest["tombstone"] is not None
    assert "tombstoned_at" in manifest["tombstone"]
    assert "deletable_after" in manifest["tombstone"]

    jsonschema.validate(instance=manifest, schema=schema)


def test_get_dataset_by_id_unknown_returns_404(catalog_api_lake):
    client = TestClient(app)
    resp_not_found = client.get("/api/v1/catalog/datasets/00000000-0000-0000-0000-000000000000")
    assert resp_not_found.status_code == 404

    resp_invalid_uuid = client.get("/api/v1/catalog/datasets/invalid-uuid")
    assert resp_invalid_uuid.status_code == 404


def test_operational_error_returns_503_and_leaves_lake_unchanged(catalog_api_lake):
    dst_lake, sm, catalog = catalog_api_lake

    # Snapshot lake files before
    snapshot_before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in dst_lake.rglob("*") if p.is_file()}

    client = TestClient(app)

    def failing_session_factory():
        raise OperationalError("Connection refused", None, None)

    with patch.object(catalog, "session_factory", side_effect=failing_session_factory):
        # 1. list endpoint
        resp_list = client.get("/api/v1/catalog/datasets")
        assert resp_list.status_code == 503
        assert resp_list.json().get("code") == "catalog_unavailable"

        # 2. ingest endpoint
        resp_ingest = client.post(
            "/api/v1/storage/ingest",
            json={
                "symbol": "PETR4",
                "timeframes": ["D1"],
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
        )
        assert resp_ingest.status_code == 503
        assert resp_ingest.json().get("code") == "catalog_unavailable"

        # 3. delete endpoint
        resp_delete = client.delete("/api/v1/storage/PETR4/D1")
        assert resp_delete.status_code == 503
        assert resp_delete.json().get("code") == "catalog_unavailable"

    # Snapshot lake files after
    snapshot_after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in dst_lake.rglob("*") if p.is_file()}
    assert snapshot_after == snapshot_before
