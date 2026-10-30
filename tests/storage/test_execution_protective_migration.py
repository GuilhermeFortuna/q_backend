from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from q_backend.storage.db.migrations import alembic_ini_path
from q_backend.storage.settings import get_settings

ORDER_COLUMNS = {"intent_kind", "price", "stop_loss", "take_profit", "expires_at", "external_position_id"}
POSITION_COLUMNS = {"stop_loss", "take_profit", "external_position_id"}


def test_protective_order_columns_upgrade_and_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_url = f"sqlite:///{tmp_path / 'protective.db'}"
    monkeypatch.setenv("Q_DATABASE_URL", db_url)
    get_settings.cache_clear()

    alembic_cfg = Config(str(alembic_ini_path()))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    engine = create_engine(db_url)

    command.upgrade(alembic_cfg, "20261029_0032")
    before_orders = {column["name"] for column in inspect(engine).get_columns("execution_orders")}
    assert not (ORDER_COLUMNS & before_orders)

    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    order_columns = {column["name"] for column in inspector.get_columns("execution_orders")}
    position_columns = {column["name"] for column in inspector.get_columns("execution_net_positions")}
    assert ORDER_COLUMNS <= order_columns
    assert POSITION_COLUMNS <= position_columns

    command.downgrade(alembic_cfg, "20261029_0032")
    inspector = inspect(engine)
    assert not (ORDER_COLUMNS & {column["name"] for column in inspector.get_columns("execution_orders")})
    assert not (POSITION_COLUMNS & {column["name"] for column in inspector.get_columns("execution_net_positions")})
    engine.dispose()
    get_settings.cache_clear()
