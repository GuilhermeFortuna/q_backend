from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.optimization.config_loader import load_optimization_config
from q_backend.storage.db.base import Base
from q_backend.storage.settings import get_settings

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "optimization"


@pytest.fixture
def sample_ohlcv_df():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for day in range(120):
        for hour in (0, 6, 12, 18):
            timestamp = base + timedelta(days=day, hours=hour)
            drift = 0.1 if day % 10 < 5 else -0.05
            price = max(50.0, price + drift)
            rows.append(
                {
                    "time": timestamp,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 1000,
                }
            )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


@pytest.fixture
def single_objective_config():
    return load_optimization_config(FIXTURES_DIR / "single_objective.yaml")


@pytest.fixture
def multi_objective_config():
    return load_optimization_config(FIXTURES_DIR / "multi_objective.yaml")


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    monkeypatch.delenv("DATA_LAKE_ROOT", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
