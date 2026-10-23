"""Stored backtest results gain ``performance`` on read, without a write-back (Q-123)."""

import json
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.api.routers.backtest import get_backtest_result
from q_backend.api.schemas.backtest import BacktestResponse
from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.storage.db.models import RunStatus
from q_backend.storage.db.repositories import create_backtest_run
from q_backend.storage.lake.artifacts import write_backtest_result
from q_backend.storage.settings import get_settings
from test_backtest_persistence import (  # noqa: F401 - fixtures
    api_db_engine,
    api_db_session,
    api_session_factory,
    api_session_scope,
)


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path / "lake"))
    get_settings.cache_clear()
    yield tmp_path / "lake"
    get_settings.cache_clear()


@pytest.fixture
def run_service_db(api_session_scope):
    """Route the run service's config lookup to the test database."""
    with patch("q_backend.backtesting.run_service.session_scope", api_session_scope):
        yield


def _trade(pnl: float, entry: datetime, exit_: datetime) -> dict:
    trade = Trade(
        id=str(uuid.uuid4()),
        order_id=str(uuid.uuid4()),
        symbol="WIN$N",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_time=entry,
        entry_price=100.0,
        exit_time=exit_,
        exit_price=100.0 + pnl,
        status=TradeStatus.CLOSED,
        pnl=pnl,
        exit_reason="tp" if pnl > 0 else "sl",
    )
    return trade.model_dump(mode="json")


def _stored_run(api_session_scope, config: dict, trades: list[dict], extra: dict | None = None) -> str:
    """Create a completed run row and a result file that predates ``performance``."""
    with api_session_scope() as session:
        run = create_backtest_run(
            session,
            backtest_config_id=uuid.uuid4(),
            config=config,
            status=RunStatus.COMPLETED.value,
            origin="script",
        )
        run_id = str(run.id)
    write_backtest_result(
        run_id,
        {
            "metrics": {"total_pnl": 0.0},
            "trades": trades,
            "bars": [],
            "indicators": [],
            "run_id": run_id,
            **(extra or {}),
        },
    )
    return run_id


def test_stored_result_without_performance_is_computed_on_read(lake, api_session_scope, run_service_db):
    trades = [
        _trade(200.0, datetime(2024, 1, 2), datetime(2024, 1, 4)),
        _trade(-50.0, datetime(2024, 1, 6), datetime(2024, 1, 8)),
    ]
    config = {
        "symbol": "WIN$N",
        "initial_capital": 10_000.0,
        "start": "2024-01-01T00:00:00",
        "end": "2024-01-31T00:00:00",
    }
    run_id = _stored_run(api_session_scope, config, trades)
    stored_path = lake / "backtests" / run_id / "result.json"
    before = stored_path.read_bytes()

    payload = get_backtest_result(run_id)

    performance = payload["performance"]
    assert performance["portfolio"]["net_pnl"] == pytest.approx(150.0)
    assert performance["portfolio"]["max_drawdown_value"] == pytest.approx(50.0)
    assert performance["trades"]["all"]["total"] == 2
    assert performance["basis"] == {"equity": "closed_trade", "periods_per_year": 252, "risk_free_rate": 0.0}
    BacktestResponse.model_validate(payload)
    assert stored_path.read_bytes() == before
    assert "performance" not in json.loads(before)


def test_run_without_initial_capital_serves_null_performance(lake, api_session_scope, run_service_db):
    trades = [_trade(10.0, datetime(2024, 1, 2), datetime(2024, 1, 3))]
    run_id = _stored_run(api_session_scope, {"symbol": "WIN$N"}, trades)

    payload = get_backtest_result(run_id)

    assert payload["performance"] is None
    assert payload["metrics"] == {"total_pnl": 0.0}
    BacktestResponse.model_validate(payload)


def test_malformed_stored_trades_degrade_to_null(lake, api_session_scope, run_service_db):
    run_id = _stored_run(api_session_scope, {"initial_capital": 10_000.0}, [{"pnl": 1.0}])

    payload = get_backtest_result(run_id)

    assert payload["performance"] is None


def test_stored_performance_is_served_unchanged(lake, api_session_scope, run_service_db):
    marker = {"sentinel": True}
    run_id = _stored_run(api_session_scope, {"initial_capital": 10_000.0}, [], extra={"performance": marker})

    payload = get_backtest_result(run_id)

    assert payload["performance"] == marker
