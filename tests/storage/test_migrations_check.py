from __future__ import annotations

from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from q_backend.storage.db.migrations import SchemaRevision, alembic_ini_path, schema_revision
from q_backend.storage.settings import get_settings


def test_alembic_ini_path_exists():
    ini = alembic_ini_path()
    assert ini.is_file()
    assert ini.name == "alembic.ini"


def test_schema_revision_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "test_migrations.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("Q_DATABASE_URL", db_url)
    get_settings.cache_clear()

    ini_path = alembic_ini_path()
    engine = create_engine(db_url)

    # Before migration: current is None, head is valid string
    initial_rev = schema_revision(engine, ini_path)
    assert isinstance(initial_rev, SchemaRevision)
    assert initial_rev.current is None
    assert isinstance(initial_rev.head, str)
    assert len(initial_rev.head) > 0

    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # After alembic upgrade head: current == head
    command.upgrade(alembic_cfg, "head")
    rev_after_upgrade = schema_revision(engine, ini_path)
    assert rev_after_upgrade.current == rev_after_upgrade.head
    assert rev_after_upgrade.current is not None

    # After alembic downgrade -1: current equals previous revision id and differs from head
    command.downgrade(alembic_cfg, "-1")
    rev_after_downgrade = schema_revision(engine, ini_path)
    assert rev_after_downgrade.current != rev_after_downgrade.head
    assert rev_after_downgrade.current is not None
    assert rev_after_downgrade.head == rev_after_upgrade.head
