"""Multi-entry backtest API tests (WO109)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.backtest_jobs import BacktestJobRequest, _execute_candle
from q_backend.api.routers.backtest import get_backtest, start_backtest
from q_backend.api.routers.strategies import list_signal_managers_catalog
from q_backend.backtesting.entry_config import normalize_entries
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from backtest_test_helpers import mock_worker_market_service, run_async_backtest

MULTI_ENTRY_REQUEST = {
    "symbol": "WIN$",
    "timeframe": "M5",
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-02-01T00:00:00Z",
    "initial_capital": 100000.0,
    "point_value": 0.2,
    "entries": [
        {
            "strategy": "MACrossover",
            "params": {"short_period": 10, "long_period": 30},
        },
        {
            "strategy": "RSIMeanReversion",
            "params": {"period": 14},
        },
    ],
    "entry_manager": {"kind": "majority", "params": {"vote_threshold": 2}},
    "exit_params": {"stop_loss_atr": 2.0, "atr_period": 14},
}


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
    for index in range(30):
        close = 100.0 + (index % 5)
        bars.append(
            OHLCV(
                time=base + timedelta(days=index),
                open=close - 1,
                high=close + 2,
                low=close - 2,
                close=close,
                tick_volume=1000,
            )
        )
    return bars


def _trade_signature(trades: list[dict]) -> list[tuple]:
    return [
        (
            trade["action"],
            trade["entry_time"],
            trade["entry_price"],
            trade.get("exit_time"),
            trade.get("exit_price"),
        )
        for trade in trades
    ]


def test_multi_entry_backtest_persists_config_and_display_label(
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv
):
    run_id, result = run_async_backtest(
        MULTI_ENTRY_REQUEST,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
    )

    assert result["metrics"]["total_trades"] >= 0

    detail = get_backtest(run_id, session=api_db_session)
    assert detail.strategy == "MACrossover+RSIMeanReversion (majority)"
    assert detail.config["entries"] == MULTI_ENTRY_REQUEST["entries"]
    assert detail.config["entry_manager"] == MULTI_ENTRY_REQUEST["entry_manager"]
    assert detail.config["exit_params"] == MULTI_ENTRY_REQUEST["exit_params"]


def test_legacy_equivalence_between_strategy_params_and_entries_shape(
    sample_ohlcv,
):
    legacy_request = BacktestJobRequest(
        symbol="BTCUSDT",
        timeframe="D1",
        start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end=datetime(2023, 3, 1, tzinfo=timezone.utc),
        strategy="MACrossover",
        strategy_params={
            "short_period": 5,
            "long_period": 10,
            "stop_loss_pct": 0.02,
        },
    )
    entries_request = BacktestJobRequest(
        symbol="BTCUSDT",
        timeframe="D1",
        start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end=datetime(2023, 3, 1, tzinfo=timezone.utc),
        entries=[
            {
                "strategy": "MACrossover",
                "params": {"short_period": 5, "long_period": 10},
            }
        ],
        entry_manager={"kind": "or", "params": {}},
        exit_params={"stop_loss_pct": 0.02},
    )

    mock_service = mock_worker_market_service(ohlcv=sample_ohlcv)
    legacy_payload, _, _ = _execute_candle(
        legacy_request,
        legacy_request.start,
        legacy_request.end,
        mock_service,
    )
    entries_payload, _, _ = _execute_candle(
        entries_request,
        entries_request.start,
        entries_request.end,
        mock_service,
    )

    assert _trade_signature(legacy_payload["trades"]) == _trade_signature(
        entries_payload["trades"]
    )
    assert legacy_payload["indicators"] == entries_payload["indicators"]


def test_normalize_entries_splits_legacy_exit_params():
    request = BacktestJobRequest(
        symbol="WIN$",
        strategy="MACrossover",
        strategy_params={
            "short_period": 5,
            "long_period": 10,
            "stop_loss_pct": 0.02,
        },
    )

    entries, manager, exit_params = normalize_entries(request)

    assert len(entries) == 1
    assert entries[0].strategy == "MACrossover"
    assert entries[0].params == {"short_period": 5, "long_period": 10}
    assert manager.kind == "or"
    assert exit_params == {"stop_loss_pct": 0.02}


def test_signal_managers_catalog_lists_three_managers_with_majority_params():
    response = list_signal_managers_catalog()
    managers = response["managers"]

    assert len(managers) == 3
    ids = {manager.id for manager in managers}
    assert ids == {"or", "and", "majority"}

    majority = next(manager for manager in managers if manager.id == "majority")
    assert majority.param_names == ["vote_threshold"]
    vote_threshold = next(
        spec for spec in majority.params if spec.name == "vote_threshold"
    )
    assert vote_threshold.type == "int"
    assert vote_threshold.default == 2


def test_tick_engine_rejects_multi_entry_payload():
    request = BacktestJobRequest.model_validate(
        {
            **MULTI_ENTRY_REQUEST,
            "engine": "tick",
        }
    )

    with pytest.raises(Exception) as exc_info:
        start_backtest(request)

    assert exc_info.value.status_code == 400
    assert "candle engine" in exc_info.value.detail
