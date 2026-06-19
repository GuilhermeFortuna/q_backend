"""Candle backtest over local OHLCV store without MT5 (WO48)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers.backtest import get_backtest_result, start_backtest
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.runtime_config import set_data_source
from q_backend.storage.settings import get_settings


def _daily_bars() -> list[OHLCV]:
    bars: list[OHLCV] = []
    for day in range(1, 61):
        close = 100.0 + day * 0.5
        bars.append(
            OHLCV(
                time=datetime(2024, 1, 1) + timedelta(days=day - 1),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                tick_volume=1000,
            )
        )
    return bars


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
def local_market(tmp_path, monkeypatch):
    market = tmp_path / "market"
    runtime = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(market))
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(runtime))
    local_store.write_ohlcv("PETR4", "D1", _daily_bars())
    set_data_source("local")
    yield


def test_candle_backtest_local_mode_without_mt5(
    run_jobs_sync, local_market, api_session_scope, lake_root_path
):
    request = BacktestJobRequest(
        symbol="PETR4",
        timeframe="D1",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 2, 29),
        strategy="MACrossover",
        strategy_params={"fast_period": 5, "slow_period": 20},
    )

    with (
        patch(
            "q_backend.market_data.clients.metatrader.MetaTraderClient.get_ohlcv",
            side_effect=AssertionError("MT5 must not be called in local mode"),
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
    ):
        start_resp = start_backtest(request)
        response = get_backtest_result(start_resp["run_id"])

    assert response["metrics"]["total_trades"] >= 0
    assert response["bars"]
