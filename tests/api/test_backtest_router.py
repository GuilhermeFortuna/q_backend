"""Backtest router assembly and run-service tests (WO58)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from q_backend.api.main import app
from q_backend.api.routers import backtest as backtest_router
from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting import run_service as backtest_run_service
from q_backend.market_data.models import OHLCV

BACKTEST_ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/v1/backtest/run"),
    ("POST", "/api/v1/backtest"),
    ("GET", "/api/v1/backtest/{run_id}"),
    ("GET", "/api/v1/backtest/{run_id}/result"),
    ("GET", "/api/v1/backtests"),
    ("POST", "/api/v1/backtests/bulk-delete"),
    ("GET", "/api/v1/backtests/{run_id}"),
    ("PATCH", "/api/v1/backtests/{run_id}"),
    ("DELETE", "/api/v1/backtests/{run_id}"),
    ("GET", "/api/v1/backtests/{run_id}/artifacts/equity"),
    ("GET", "/api/v1/backtests/{run_id}/artifacts/trades"),
]

BACKTEST_OPENAPI_PATHS: list[str] = [
    "/api/v1/backtest/run",
    "/api/v1/backtest",
    "/api/v1/backtest/{run_id}",
    "/api/v1/backtest/{run_id}/result",
    "/api/v1/backtests",
    "/api/v1/backtests/bulk-delete",
    "/api/v1/backtests/{run_id}",
    "/api/v1/backtests/{run_id}/artifacts/equity",
    "/api/v1/backtests/{run_id}/artifacts/trades",
]


def _route_inventory() -> set[tuple[str, str]]:
    inventory: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            inventory.add((method, path))
    return inventory


def test_backtest_routes_exist_with_original_paths():
    inventory = _route_inventory()
    for method, path in BACKTEST_ROUTES:
        assert (method, path) in inventory


def test_backtest_openapi_paths_present():
    paths = app.openapi()["paths"]
    for path in BACKTEST_OPENAPI_PATHS:
        assert path in paths


def test_backtests_run_id_route_resolves_per_method():
    inventory = _route_inventory()
    path = "/api/v1/backtests/{run_id}"
    for method in ("GET", "PATCH", "DELETE"):
        assert (method, path) in inventory


def test_serialize_equity_artifact_matches_legacy_shape():
    df = pd.DataFrame(
        {
            "time": [datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)],
            "equity": [100_000.0],
        }
    )
    points = backtest_run_service.serialize_equity_artifact(df)
    assert points == [{"time": "2024-01-01T12:00:00Z", "equity": 100_000.0}]


def test_serialize_trades_artifact_normalizes_timestamps():
    df = pd.DataFrame(
        {
            "symbol": ["WIN$"],
            "entry_time": [pd.Timestamp("2024-01-01 10:00:00")],
            "exit_time": [None],
            "pnl": [123.45],
        }
    )
    trades = backtest_run_service.serialize_trades_artifact(df)
    assert trades[0]["symbol"] == "WIN$"
    assert trades[0]["exit_time"] is None
    assert trades[0]["entry_time"].endswith("Z")


def test_delete_backtest_lake_artifacts_swallows_errors(monkeypatch):
    calls: list[str] = []

    def _raise(run_id: str) -> None:
        calls.append(run_id)
        raise OSError("disk error")

    monkeypatch.setattr(backtest_run_service, "delete_backtest_artifacts", _raise)
    backtest_run_service.delete_backtest_lake_artifacts("abc")
    assert calls == ["abc"]


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


def test_run_backtest_handler_delegates_to_run_service(sample_ohlcv):
    request = BacktestRequest.model_validate(
        {
            "symbol": "WIN$",
            "timeframe": "M5",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-02-01T00:00:00Z",
            "strategy": "MACrossover",
            "strategy_params": {"short_period": 5, "long_period": 10},
        }
    )
    expected = {
        "metrics": {"total_pnl": 1.0},
        "trades": [],
        "bars": [],
        "indicators": [],
        "run_id": "test-run",
    }

    with patch.object(backtest_run_service, "run_sync", return_value=expected) as run_sync:
        payload = backtest_router.run_backtest(request)

    run_sync.assert_called_once_with(request)
    assert payload == expected
