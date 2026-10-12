import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.storage.db.base import Base
from q_backend.storage.settings import get_settings


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


@pytest.fixture
def quote_tick_store(tmp_path, monkeypatch):
    """Varying quotes across the leakage harness's hourly bars, in the tick store's own clock."""
    import numpy as np
    import pandas as pd
    from quote_ticks import write_quote_ticks

    from q_backend.market_data.timezone import BRASILIA_TZ

    symbol = "TEST_QUOTE"
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))

    count = 260
    close = 100.0 + np.cumsum(np.random.default_rng(20240609).normal(0.0, 1.0, size=count))
    bar_times = pd.date_range("2023-01-01", periods=count, freq="h", tz="UTC").tz_convert(BRASILIA_TZ).tz_localize(None)
    ticks = []
    for index, (bar_time, mid) in enumerate(zip(bar_times, close, strict=True)):
        # The spread differs from bar to bar, so a series shifted by one bar cannot pass.
        half = (0.2 + 0.05 * (index % 13)) / 2.0
        for offset in (10, 30, 50):
            ticks.append(
                (bar_time.to_pydatetime() + pd.Timedelta(seconds=offset), float(mid - half), float(mid + half))
            )
    write_quote_ticks(tmp_path, symbol, ticks)
    return symbol
