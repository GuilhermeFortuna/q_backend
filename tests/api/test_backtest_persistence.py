from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.main import BacktestRequest, get_backtest, list_backtests, run_backtest
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base


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


@pytest.fixture
def sample_ohlcv():
    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(30):
        close = 100.0 + (i % 5)
        bars.append(
            OHLCV(
                time=base + timedelta(days=i),
                open=close - 1,
                high=close + 2,
                low=close - 2,
                close=close,
                tick_volume=1000,
            )
        )
    return bars


def test_run_backtest_persists_and_appears_in_history(
    api_db_session, api_session_scope, sample_ohlcv
):
    request_body = {
        "symbol": "WIN$",
        "timeframe": "M5",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-02-01T00:00:00Z",
        "initial_capital": 100000.0,
        "point_value": 0.2,
        "strategy": "MACrossover",
        "strategy_params": {"short_period": 5, "long_period": 10},
    }

    with (
        patch(
            "q_backend.api.main.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.api.main.session_scope", api_session_scope),
    ):
        run_payload = run_backtest(BacktestRequest.model_validate(request_body))

    assert run_payload["run_id"] is not None
    assert "metrics" in run_payload

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert len(list_payload["items"]) == 1
    item = list_payload["items"][0]
    assert item.run_id == run_payload["run_id"]
    assert item.symbol == "WIN$"
    assert item.strategy == "MACrossover"
    assert item.status == "completed"
    assert item.summary == run_payload["metrics"]

    detail_payload = get_backtest(run_payload["run_id"], session=api_db_session)
    assert detail_payload.run_id == run_payload["run_id"]
    assert detail_payload.symbol == "WIN$"
    assert detail_payload.config["symbol"] == "WIN$"
    assert detail_payload.result_summary == run_payload["metrics"]
    assert detail_payload.error_message is None


def test_run_backtest_graceful_degradation_when_persistence_unavailable(
    sample_ohlcv,
):
    request_body = {
        "symbol": "WIN$",
        "timeframe": "M5",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-02-01T00:00:00Z",
        "initial_capital": 100000.0,
        "point_value": 0.2,
        "strategy": "MACrossover",
        "strategy_params": {"short_period": 5, "long_period": 10},
    }

    with (
        patch(
            "q_backend.api.main.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch(
            "q_backend.api.main.session_scope",
            side_effect=Exception("database unavailable"),
        ),
    ):
        payload = run_backtest(BacktestRequest.model_validate(request_body))

    assert payload["run_id"] is None
    assert "metrics" in payload
    assert "trades" in payload
