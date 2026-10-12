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
    """Write varying quote ticks across the harness's synthetic bars and point Q_RESEARCH_TICK_STORE at them."""
    from collections import defaultdict
    import numpy as np
    import pandas as pd
    from q_backend.market_data.timezone import BRASILIA_TZ
    from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

    symbol = "TEST_QUOTE"
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))

    rng = np.random.default_rng(20240609)
    n = 260
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")

    day_ticks: dict = defaultdict(lambda: {"time_msc": [], "bid": [], "ask": [], "last": [], "volume": [], "flags": []})
    for idx, (ts, c) in enumerate(zip(times, close, strict=True)):
        day = ts.tz_convert(BRASILIA_TZ).date()
        for offset_sec in (10, 30, 50):
            tick_ts = ts + pd.Timedelta(seconds=offset_sec)
            msc = int(tick_ts.timestamp() * 1000)
            spread = 0.2 + 0.05 * (idx % 13)
            bid = float(c - spread / 2.0)
            ask = float(c + spread / 2.0)
            last = float(c)
            d = day_ticks[day]
            d["time_msc"].append(msc)
            d["bid"].append(bid)
            d["ask"].append(ask)
            d["last"].append(last)
            d["volume"].append(1.0)
            d["flags"].append(6)

    directory = tmp_path / _slug_symbol(symbol)
    directory.mkdir(parents=True, exist_ok=True)
    for day, raw in day_ticks.items():
        arrays = {
            "time_msc": np.asarray(raw["time_msc"], dtype=np.int64),
            "bid": np.asarray(raw["bid"], dtype=np.float64),
            "ask": np.asarray(raw["ask"], dtype=np.float64),
            "last": np.asarray(raw["last"], dtype=np.float64),
            "volume": np.asarray(raw["volume"], dtype=np.float64),
            "flags": np.asarray(raw["flags"], dtype=np.int32),
        }
        _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)

    return symbol
