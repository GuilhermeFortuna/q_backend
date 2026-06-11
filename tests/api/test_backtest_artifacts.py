from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.main import (
    BacktestRequest,
    BulkDeleteBacktestsRequest,
    bulk_delete_backtests,
    delete_backtest,
    get_backtest,
    get_backtest_equity_artifact,
    get_backtest_trades_artifact,
    run_backtest,
)
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import BacktestRun
from q_backend.storage.lake.artifacts import lake_root
from q_backend.storage.settings import get_settings


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
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


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


def test_run_backtest_writes_lake_artifacts_and_sets_lake_paths(
    api_db_session, api_session_scope, sample_ohlcv, lake_root_path
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

    run_id = run_payload["run_id"]
    assert run_id is not None

    run = api_db_session.get(BacktestRun, uuid.UUID(run_id))
    assert run.lake_paths is not None
    assert run.lake_paths["trades"] == f"backtests/{run_id}/trades.parquet"
    assert run.lake_paths["equity"] == f"backtests/{run_id}/equity.parquet"
    assert (lake_root_path / run.lake_paths["trades"]).is_file()
    assert (lake_root_path / run.lake_paths["equity"]).is_file()

    equity_payload = get_backtest_equity_artifact(run_id)
    assert equity_payload["run_id"] == run_id
    assert len(equity_payload["points"]) >= 1
    assert "time" in equity_payload["points"][0]
    assert "equity" in equity_payload["points"][0]

    trades_payload = get_backtest_trades_artifact(run_id)
    assert trades_payload["run_id"] == run_id
    assert trades_payload["trades"] == run_payload["trades"]


def test_run_backtest_succeeds_when_lake_write_fails(
    api_db_session, api_session_scope, sample_ohlcv, lake_root_path
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
        patch(
            "q_backend.api.main.write_backtest_artifacts",
            side_effect=OSError("disk full"),
        ),
    ):
        run_payload = run_backtest(BacktestRequest.model_validate(request_body))

    assert run_payload["run_id"] is not None
    assert "metrics" in run_payload

    detail = get_backtest(run_payload["run_id"], session=api_db_session)
    assert detail.status == "completed"

    run = api_db_session.get(BacktestRun, uuid.UUID(run_payload["run_id"]))
    assert run.lake_paths is None


def test_artifact_endpoints_return_404_for_unknown_run(lake_root_path):
    unknown_id = "99999999-9999-9999-9999-999999999999"

    with pytest.raises(HTTPException) as equity_exc:
        get_backtest_equity_artifact(unknown_id)
    assert equity_exc.value.status_code == 404

    with pytest.raises(HTTPException) as trades_exc:
        get_backtest_trades_artifact(unknown_id)
    assert trades_exc.value.status_code == 404


def test_artifact_endpoints_return_404_when_files_deleted(
    api_db_session, api_session_scope, sample_ohlcv, lake_root_path
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

    run_id = run_payload["run_id"]
    artifact_dir = lake_root() / "backtests" / run_id
    for path in artifact_dir.iterdir():
        path.unlink()

    with pytest.raises(HTTPException) as exc:
        get_backtest_equity_artifact(run_id)
    assert exc.value.status_code == 404


def test_delete_backtest_removes_lake_artifacts(
    api_db_session, api_session_scope, sample_ohlcv, lake_root_path
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

    run_id = run_payload["run_id"]
    assert (lake_root() / "backtests" / run_id).is_dir()

    delete_backtest(run_id, session=api_db_session)

    assert not (lake_root() / "backtests" / run_id).exists()


def test_bulk_delete_backtests_removes_lake_artifacts(
    api_db_session, api_session_scope, sample_ohlcv, lake_root_path
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

    run_id = run_payload["run_id"]
    assert (lake_root() / "backtests" / run_id).is_dir()

    bulk_delete_backtests(
        BulkDeleteBacktestsRequest(run_ids=[run_id]),
        session=api_db_session,
    )

    assert not (lake_root() / "backtests" / run_id).exists()
