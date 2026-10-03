import csv
import io
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers.backtest import (
    bulk_delete_backtests,
    delete_backtest,
    export_backtest_market_data,
    export_backtest_trades,
    get_backtest,
    get_backtest_equity_artifact,
    get_backtest_result,
    get_backtest_trades_artifact,
    start_backtest,
)
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.schemas.common import BulkDeleteBacktestsRequest
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import BacktestRun
from q_backend.storage.lake.artifacts import lake_root
from q_backend.storage.settings import get_settings
from backtest_test_helpers import mock_worker_market_service, run_async_backtest

REQUEST_BODY = {
    "symbol": "WIN$",
    "timeframe": "M5",
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-02-01T00:00:00Z",
    "initial_capital": 100000.0,
    "point_value": 0.2,
    "strategy": "MACrossover",
    "strategy_params": {"short_period": 5, "long_period": 10},
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
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv, lake_root_path
):
    run_id, run_payload = run_async_backtest(
        REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
    )
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
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv, lake_root_path
):
    mock_service = mock_worker_market_service(ohlcv=sample_ohlcv)
    with (
        patch(
            "q_backend.tasks.worker_context.get_worker_market_data_service",
            return_value=mock_service,
        ),
        patch("q_backend.api.backtest_jobs.session_scope", api_session_scope),
        patch(
            "q_backend.api.backtest_jobs.write_backtest_artifacts",
            side_effect=OSError("disk full"),
        ),
    ):
        start_resp = start_backtest(BacktestJobRequest.model_validate(REQUEST_BODY))

    run_id = start_resp["run_id"]
    assert run_id is not None

    detail = get_backtest(run_id, session=api_db_session)
    assert detail.status == "completed"

    run = api_db_session.get(BacktestRun, uuid.UUID(run_id))
    assert run.lake_paths is None

    with pytest.raises(HTTPException) as exc:
        get_backtest_result(run_id)
    assert exc.value.status_code == 404


def test_artifact_endpoints_return_404_for_unknown_run(lake_root_path):
    unknown_id = "99999999-9999-9999-9999-999999999999"

    with pytest.raises(HTTPException) as equity_exc:
        get_backtest_equity_artifact(unknown_id)
    assert equity_exc.value.status_code == 404

    with pytest.raises(HTTPException) as trades_exc:
        get_backtest_trades_artifact(unknown_id)
    assert trades_exc.value.status_code == 404


def test_artifact_endpoints_return_404_when_files_deleted(
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv, lake_root_path
):
    run_id, _run_payload = run_async_backtest(
        REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
    )

    artifact_dir = lake_root() / "backtests" / run_id
    for path in artifact_dir.iterdir():
        path.unlink()

    with pytest.raises(HTTPException) as exc:
        get_backtest_equity_artifact(run_id)
    assert exc.value.status_code == 404


def test_delete_backtest_removes_lake_artifacts(
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv, lake_root_path
):
    run_id, _run_payload = run_async_backtest(
        REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
    )
    assert (lake_root() / "backtests" / run_id).is_dir()

    delete_backtest(run_id, session=api_db_session)

    assert not (lake_root() / "backtests" / run_id).exists()


def test_bulk_delete_backtests_removes_lake_artifacts(
    run_jobs_sync, api_db_session, api_session_scope, sample_ohlcv, lake_root_path
):
    run_id, _run_payload = run_async_backtest(
        REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=sample_ohlcv,
    )
    assert (lake_root() / "backtests" / run_id).is_dir()

    bulk_delete_backtests(
        BulkDeleteBacktestsRequest(run_ids=[run_id]),
        session=api_db_session,
    )

    assert not (lake_root() / "backtests" / run_id).exists()


# Exit-rule params ride in ``strategy_params`` for single-strategy requests.
EXPORT_REQUEST_BODY = {
    **REQUEST_BODY,
    "timeframe": "D1",
    "end": "2024-03-01T00:00:00Z",
    "strategy_params": {"short_period": 2, "long_period": 4, "stop_loss_atr": 2.0, "atr_period": 3},
}


@pytest.fixture
def trending_ohlcv():
    """Up, down, then up again so the MA crossover opens and closes a trade."""
    base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    closes = [100.0 + i for i in range(15)] + [114.0 - 2 * i for i in range(15)] + [86.0 + 2 * i for i in range(15)]
    return [
        OHLCV(
            time=base + timedelta(days=i),
            open=close - 1,
            high=close + 2,
            low=close - 2,
            close=close,
            tick_volume=1000,
        )
        for i, close in enumerate(closes)
    ]


def _csv_rows(response) -> list[dict[str, str]]:
    assert response.media_type == "text/csv"
    return list(csv.DictReader(io.StringIO(response.body.decode("utf-8"))))


def test_export_market_data_csv_has_bars_and_every_indicator(
    run_jobs_sync, api_db_session, api_session_scope, trending_ohlcv, lake_root_path
):
    run_id, run_payload = run_async_backtest(
        EXPORT_REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=trending_ohlcv,
    )

    run = api_db_session.get(BacktestRun, uuid.UUID(run_id))
    assert run.lake_paths["market_data"] == f"backtests/{run_id}/market_data.parquet"

    rows = _csv_rows(export_backtest_market_data(run_id))
    columns = list(rows[0])

    assert len(rows) == len(run_payload["bars"])
    assert columns[0] == "time"
    assert [row["time"] for row in rows] == [bar["timestamp"] for bar in run_payload["bars"]]
    assert {"open", "high", "low", "close"} <= set(columns)
    for indicator in run_payload["indicators"]:
        assert indicator["key"] in columns
    # Exit-rule columns are computed outside the strategy and never reach the chart.
    assert "atr_3" in columns
    assert "atr_3" not in {indicator["key"] for indicator in run_payload["indicators"]}
    assert not [column for column in columns if column.startswith("q_signal_") or column == "bar_index"]


def test_export_trades_csv_columns_and_values(
    run_jobs_sync, api_db_session, api_session_scope, trending_ohlcv, lake_root_path
):
    run_id, run_payload = run_async_backtest(
        EXPORT_REQUEST_BODY,
        api_session_scope=api_session_scope,
        sample_ohlcv=trending_ohlcv,
    )
    assert run_payload["trades"]

    response = export_backtest_trades(run_id)
    header = response.body.decode("utf-8").splitlines()[0]
    rows = _csv_rows(response)

    assert header == (
        "trade_id,symbol,side,entry_time,entry_price,exit_time,exit_price,pnl,"
        "quantity,commission,point_value,exit_reason"
    )
    assert len(rows) == len(run_payload["trades"])
    first, expected = rows[0], run_payload["trades"][0]
    assert first["trade_id"] == expected["id"]
    assert first["side"] == expected["action"]
    assert first["entry_time"] == expected["entry_time"]
    assert float(first["pnl"]) == pytest.approx(expected["pnl"])


@pytest.mark.parametrize("export", [export_backtest_market_data, export_backtest_trades])
def test_export_csv_returns_404_without_artifact(export, lake_root_path):
    with pytest.raises(HTTPException) as exc_info:
        export(str(uuid.uuid4()))
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        export("not-a-uuid")
    assert exc_info.value.status_code == 404
