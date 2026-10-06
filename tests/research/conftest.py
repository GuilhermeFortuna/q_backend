"""Shared fixtures for research library tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.research.providers import ResearchResources
from q_backend.storage.db.base import Base


@pytest.fixture
def lake_catalog(tmp_path: Path) -> tuple[Path, LakeCatalog, sessionmaker]:
    src_fixture = Path(__file__).resolve().parents[1] / "fixtures/lake"
    dst_lake = tmp_path / "lake"
    shutil.copytree(src_fixture, dst_lake)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    catalog = LakeCatalog(session_factory=sm, root=dst_lake)
    catalog.adopt()
    return dst_lake, catalog, sm


@pytest.fixture
def research_resources(lake_catalog) -> ResearchResources:
    lake_path, catalog, sm = lake_catalog
    return ResearchResources(
        database_url="sqlite:///:memory:",
        market_data_root=lake_path,
        gateway_url=None,
        gateway_token=None,
        session_factory=sm,
        catalog=catalog,
    )
