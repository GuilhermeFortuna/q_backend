import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from q_backend.storage.db.migrations import alembic_ini_path
from q_backend.storage.settings import get_settings


def test_backtest_run_origin_backfills_existing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_url = f"sqlite:///{tmp_path / 'origin.db'}"
    monkeypatch.setenv("Q_DATABASE_URL", db_url)
    get_settings.cache_clear()

    alembic_cfg = Config(str(alembic_ini_path()))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    engine = create_engine(db_url)

    command.upgrade(alembic_cfg, "20261003_0026")
    with engine.begin() as conn:
        configs = sa.Table("backtest_configs", sa.MetaData(), autoload_with=conn)
        runs = sa.Table("backtest_runs", sa.MetaData(), autoload_with=conn)
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        config_id = uuid.uuid4()
        conn.execute(
            sa.insert(configs).values(id=config_id.hex, name="WIN$-M5", config={}, created_at=now, updated_at=now)
        )
        conn.execute(
            sa.insert(runs).values(
                id=uuid.uuid4().hex,
                backtest_config_id=config_id.hex,
                status="completed",
                config={},
                created_at=now,
                updated_at=now,
            )
        )

    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        origins = conn.execute(sa.text("SELECT origin FROM backtest_runs")).scalars().all()
    assert origins == ["stack"]

    command.downgrade(alembic_cfg, "20261003_0026")
    columns = {column["name"] for column in inspect(engine).get_columns("backtest_runs")}
    assert "origin" not in columns
    assert "provenance" not in columns
    engine.dispose()
    get_settings.cache_clear()
