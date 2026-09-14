"""CLI tests for the lake dataset catalog CLI (q-catalog)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.cli import q_catalog
from q_backend.market_data.catalog import service as catalog_service
from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.storage.db.base import Base


@pytest.fixture
def cli_lake(tmp_path: Path, monkeypatch) -> tuple[Path, sessionmaker, LakeCatalog]:
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
    monkeypatch.setattr(catalog_service, "_catalog_instance", catalog)
    monkeypatch.setattr(q_catalog, "get_lake_catalog", lambda: catalog)
    return dst_lake, sm, catalog


def test_cli_adopt_reports_datasets_files_bytes(cli_lake, capsys):
    code = q_catalog.main(["adopt"])
    assert code == 0

    out = capsys.readouterr().out
    assert "Adopted:" in out
    assert "bars:" in out
    assert "ticks:" in out
    assert "Files:" in out
    assert "Bytes:" in out

    # Second adopt reports skipped
    code2 = q_catalog.main(["adopt"])
    assert code2 == 0
    out2 = capsys.readouterr().out
    assert "Skipped (already cataloged): 3" in out2


def test_cli_show_valid_and_invalid(cli_lake, capsys):
    dst_lake, sm, catalog = cli_lake
    q_catalog.main(["adopt"])
    capsys.readouterr()  # clear adopt output

    with sm() as session:
        ds = current_dataset(session, Subject(kind="bars", symbol="PETR4", timeframe="D1"))
        assert ds is not None
        target_id = str(ds.dataset_id)

    code = q_catalog.main(["show", target_id])
    assert code == 0
    out = capsys.readouterr().out
    manifest = json.loads(out)
    assert manifest["dataset_id"] == target_id
    assert manifest["subject"]["symbol"] == "PETR4"

    # Unknown UUID
    code_unknown = q_catalog.main(["show", "00000000-0000-0000-0000-000000000000"])
    assert code_unknown != 0

    # Malformed UUID
    code_malformed = q_catalog.main(["show", "not-a-uuid"])
    assert code_malformed != 0


def test_cli_sweep_dry_run_and_active(cli_lake, capsys):
    q_catalog.main(["adopt"])
    capsys.readouterr()

    # Dry-run sweep
    code_dry = q_catalog.main(["sweep", "--dry-run"])
    assert code_dry == 0
    out_dry = capsys.readouterr().out
    assert "Dry-run sweep report" in out_dry

    # Real sweep
    code_real = q_catalog.main(["sweep"])
    assert code_real == 0
    out_real = capsys.readouterr().out
    assert "Sweep report" in out_real
