"""Tick backtest over local tick store without MT5 (WO50)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers.backtest import (
    get_backtest_result,
    get_backtest_status,
    start_backtest,
)
from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.storage.db.base import Base
from q_backend.storage.runtime_config import set_data_source
from q_backend.storage.settings import get_settings


def _synthetic_ticks(n: int = 500) -> dict[str, np.ndarray]:
    base = datetime(2024, 1, 2, 10, 0, 0)
    time_msc = np.array(
        [_naive_local_to_time_msc(base + timedelta(seconds=i)) for i in range(n)],
        dtype=np.int64,
    )
    prices = 100.0 + np.cumsum(np.sin(np.linspace(0, 12, n)) * 0.05)
    return {
        "time_msc": time_msc,
        "bid": (prices - 0.01).astype(np.float64),
        "ask": (prices + 0.01).astype(np.float64),
        "last": prices.astype(np.float64),
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.zeros(n, dtype=np.int32),
    }


@pytest.fixture
def api_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    @contextmanager
    def test_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield test_session_scope
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def local_tick_market(tmp_path, monkeypatch):
    market = tmp_path / "market"
    runtime = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(market))
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(runtime))
    local_store.write_ticks("WIN$", _synthetic_ticks())
    set_data_source("local")
    yield


def test_tick_backtest_local_mode_without_mt5(run_jobs_sync, local_tick_market, api_session_scope, lake_root_path):
    request = BacktestJobRequest(
        symbol="WIN$",
        engine="tick",
        display_timeframe="M1",
        start=datetime(2024, 1, 2, 10, 0, 0),
        end=datetime(2024, 1, 2, 10, 8, 0),
        initial_capital=100_000.0,
        point_value=0.2,
        strategy="TickMaBreakout",
        strategy_params={
            "short_period": 5,
            "long_period": 20,
            "threshold": 0.0,
            "sl_points": 1.0,
            "tp_points": 2.0,
        },
    )

    with patch("q_backend.api.backtest_jobs.session_scope", api_session_scope):
        start_resp = start_backtest(request)
        response = get_backtest_result(start_resp["run_id"])

    assert "total_pnl" in response["metrics"]
    assert isinstance(response["bars"], list)
    assert len(response["bars"]) > 0


def test_tick_backtest_local_mode_missing_ticks_returns_404(
    run_jobs_sync, local_tick_market, api_session_scope, lake_root_path
):
    request = BacktestJobRequest(
        symbol="MISSING",
        engine="tick",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 1, 2),
        strategy="TickMaBreakout",
        strategy_params={"short_period": 5, "long_period": 10},
    )

    with patch("q_backend.api.backtest_jobs.session_scope", api_session_scope):
        start_resp = start_backtest(request)
        status = get_backtest_status(start_resp["run_id"])
    assert status["status"] == "failed"

    with pytest.raises(HTTPException) as exc:
        get_backtest_result(start_resp["run_id"])

    assert exc.value.status_code == 404
