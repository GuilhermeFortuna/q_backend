from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers.backtest import (
    get_backtest,
    get_backtest_result,
    get_backtest_status,
    list_backtests,
    start_backtest,
)
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.storage.db.base import Base
from backtest_test_helpers import mock_worker_market_service, run_async_backtest


@pytest.fixture
def api_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_session_factory(api_db_engine):
    return sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def api_session_scope(api_session_factory):
    @contextmanager
    def test_session_scope():
        session = api_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return test_session_scope


@pytest.fixture
def api_db_session(api_session_factory) -> Session:
    session = api_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _sample_columnar(n: int = 120) -> dict[str, np.ndarray]:
    base_msc = int(datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc).timestamp()) * 1000
    time_msc = base_msc + np.arange(n, dtype=np.int64) * 1000
    last = 100.0 + np.arange(n) * 0.01
    return {
        "time_msc": time_msc,
        "bid": last - 0.01,
        "ask": last + 0.01,
        "last": last.astype(np.float64),
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.zeros(n, dtype=np.int32),
    }


def test_tick_backtest_returns_metrics_trades_bars_and_run_id(
    run_jobs_sync, api_db_session, api_session_scope
):
    request_body = {
        "symbol": "WIN$",
        "engine": "tick",
        "display_timeframe": "M1",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
        "initial_capital": 100000.0,
        "point_value": 0.2,
        "strategy": "TickMaBreakout",
        "strategy_params": {
            "short_period": 5,
            "long_period": 10,
            "threshold": 0.0,
            "sl_points": 1.0,
            "tp_points": 2.0,
        },
    }

    run_id, payload = run_async_backtest(
        request_body,
        api_session_scope=api_session_scope,
        ticks_columnar=_sample_columnar(),
    )

    assert run_id is not None
    assert "total_pnl" in payload["metrics"]
    assert isinstance(payload["trades"], list)
    assert isinstance(payload["bars"], list)
    assert len(payload["bars"]) > 0
    assert isinstance(payload["indicators"], list)
    assert len(payload["indicators"]) > 0
    for bar in payload["bars"]:
        assert bar["timestamp"].endswith("Z")

    if payload["trades"]:
        trade = payload["trades"][0]
        assert "entry_price" in trade
        assert "exit_price" in trade

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert list_payload["items"][0].timeframe == "TICK"

    detail = get_backtest(run_id, session=api_db_session)
    assert detail.timeframe == "TICK"
    assert detail.config["engine"] == "tick"
    assert detail.config["display_timeframe"] == "M1"
    assert detail.config["timeframe"] == "TICK"


def test_tick_backtest_no_ticks_returns_404(run_jobs_sync, api_session_scope):
    request = BacktestJobRequest.model_validate(
        {
            "symbol": "WIN$",
            "engine": "tick",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "strategy": "TickMaBreakout",
            "strategy_params": {"short_period": 5, "long_period": 10},
        }
    )
    mock_service = mock_worker_market_service(
        ticks_columnar={
            "time_msc": np.array([], dtype=np.int64),
            "bid": np.array([], dtype=np.float64),
            "ask": np.array([], dtype=np.float64),
            "last": np.array([], dtype=np.float64),
            "volume": np.array([], dtype=np.float64),
            "flags": np.array([], dtype=np.int32),
        }
    )

    with (
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=mock_service,
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
    ):
        start_resp = start_backtest(request)

    run_id = start_resp["run_id"]
    status = get_backtest_status(run_id)
    assert status["status"] == "failed"

    with pytest.raises(HTTPException) as exc:
        get_backtest_result(run_id)
    assert exc.value.status_code == 404


def test_tick_backtest_mt5_offline_returns_503(run_jobs_sync, api_session_scope):
    request = BacktestJobRequest.model_validate(
        {
            "symbol": "WIN$",
            "engine": "tick",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "strategy": "TickMaBreakout",
        }
    )
    mock_service = mock_worker_market_service()
    mock_service.get_ticks_columnar.side_effect = ConnectionError(
        "MetaTrader 5 terminal is offline."
    )

    with (
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=mock_service,
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
    ):
        start_resp = start_backtest(request)

    run_id = start_resp["run_id"]
    status = get_backtest_status(run_id)
    assert status["status"] == "failed"
    assert "offline" in (status.get("error") or "").lower()

    with pytest.raises(HTTPException) as exc:
        get_backtest_result(run_id)
    assert exc.value.status_code == 404
