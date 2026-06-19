from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers.optimization import bulk_delete_optimizations
from q_backend.api.routers.backtest import (
    bulk_delete_backtests,
    delete_backtest,
    get_backtest,
    list_backtests,
    patch_backtest,
    run_backtest,
)
from q_backend.api.schemas.backtest import BacktestRequest, BacktestRunPatchRequest
from q_backend.api.schemas.common import (
    BulkDeleteBacktestsRequest,
    BulkDeleteOptimizationsRequest,
)
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch(
            "q_backend.backtesting.run_service.session_scope",
            side_effect=Exception("database unavailable"),
        ),
    ):
        payload = run_backtest(BacktestRequest.model_validate(request_body))

    assert payload["run_id"] is None
    assert "metrics" in payload
    assert "trades" in payload


def test_delete_backtest_removes_run_from_history(
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
    ):
        run_payload = run_backtest(BacktestRequest.model_validate(request_body))

    run_id = run_payload["run_id"]
    assert run_id is not None

    delete_backtest(run_id, session=api_db_session)

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0
    assert list_payload["items"] == []


def test_list_backtests_filters_sort_and_patch_save(
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
    ):
        run_payload = run_backtest(BacktestRequest.model_validate(request_body))

    run_id = run_payload["run_id"]
    assert run_id is not None

    filtered = list_backtests(
        session=api_db_session,
        limit=50,
        offset=0,
        strategy="MACrossover",
        sort="pnl_desc",
    )
    assert filtered["total"] == 1
    assert filtered["items"][0].is_saved is False

    saved = patch_backtest(
        run_id,
        BacktestRunPatchRequest(is_saved=True),
        session=api_db_session,
    )
    assert saved.is_saved is True

    saved_only = list_backtests(session=api_db_session, limit=50, offset=0, saved_only=True)
    assert saved_only["total"] == 1
    assert saved_only["items"][0].run_id == run_id


def test_bulk_delete_backtests(api_db_session, api_session_scope, sample_ohlcv):
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
    ):
        run_payload = run_backtest(BacktestRequest.model_validate(request_body))

    run_id = run_payload["run_id"]
    assert run_id is not None

    result = bulk_delete_backtests(
        BulkDeleteBacktestsRequest(run_ids=[run_id, "not-a-uuid"]),
        session=api_db_session,
    )
    assert result["deleted"] == 1
    assert result["not_found"] == ["not-a-uuid"]

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 0


def test_run_backtest_reuses_existing_history_entry_for_identical_config(
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
    request = BacktestRequest.model_validate(request_body)

    with (
        patch(
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
    ):
        first_payload = run_backtest(request)
        second_payload = run_backtest(request)

    assert first_payload["run_id"] is not None
    assert second_payload["run_id"] == first_payload["run_id"]

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 1
    assert list_payload["items"][0].run_id == first_payload["run_id"]


def test_run_backtest_creates_separate_history_for_different_config(
    api_db_session, api_session_scope, sample_ohlcv
):
    base_request = {
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
            "q_backend.backtesting.run_service.market_data_service.get_ohlcv",
            return_value=sample_ohlcv,
        ),
        patch("q_backend.backtesting.run_service.session_scope", api_session_scope),
    ):
        first_payload = run_backtest(BacktestRequest.model_validate(base_request))
        second_payload = run_backtest(
            BacktestRequest.model_validate(
                {**base_request, "strategy_params": {"short_period": 7, "long_period": 14}}
            )
        )

    assert first_payload["run_id"] != second_payload["run_id"]

    list_payload = list_backtests(session=api_db_session, limit=50, offset=0)
    assert list_payload["total"] == 2
