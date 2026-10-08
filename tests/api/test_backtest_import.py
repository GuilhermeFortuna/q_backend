"""Backtest run import: validation, persistence and read-back through the existing endpoints."""

import copy
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from q_backend.api.routers.backtest import (
    export_backtest_market_data,
    export_backtest_trades,
    get_backtest,
    get_backtest_equity_artifact,
    get_backtest_result,
    get_backtest_trades_artifact,
    import_backtest,
    list_backtests,
)
from q_backend.api.schemas.backtest import BacktestImportRequest
from q_backend.storage.settings import get_settings
from test_backtest_persistence import (  # noqa: F401 - fixtures
    api_db_engine,
    api_db_session,
    api_session_factory,
    api_session_scope,
)

BASE_EPOCH = 1704193200  # 2024-01-02 11:00 Brasília (naive wall clock in the stack's convention)

TRADE = {
    "id": "trade-1",
    "order_id": "order-1",
    "symbol": "WIN$N",
    "action": "BUY",
    "quantity": 1.0,
    "entry_time": "2024-01-02T11:00:00-03:00",
    "entry_price": 128500.0,
    "exit_time": "2024-01-02T11:10:00-03:00",
    "exit_price": 129000.0,
    "status": "CLOSED",
    "pnl": 500.0,
    "commission": 0.0,
    "point_value": 0.2,
    "exit_reason": "signal",
}


@pytest.fixture
def lake(tmp_path, monkeypatch):
    root = tmp_path / "lake"
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _bars(count: int = 5) -> list[dict]:
    return [
        {
            "timestamp": BASE_EPOCH + 300 * i,
            "open": 128500.0 + i,
            "high": 128520.0 + i,
            "low": 128490.0 + i,
            "close": 128510.0 + i,
            "volume": 1200,
        }
        for i in range(count)
    ]


def _body() -> dict:
    return {
        "config": {
            "symbol": "WIN$N",
            "timeframe": "M5",
            "strategy": "ScriptStrategy",
            "strategy_params": {"fast_period": 9},
            "start": "2024-01-02T10:00:00-03:00",
            "end": "2024-01-02T17:00:00-03:00",
            "engine": "candle",
            "initial_capital": 100000.0,
            "point_value": 0.2,
        },
        "result": {
            "run_id": "550e8400-e29b-41d4-a716-446655440000",
            "metrics": {"total_pnl": 500.0, "trade_count": 1},
            "trades": [copy.deepcopy(TRADE)],
            "bars": _bars(),
            "indicators": [
                {
                    "key": "ma_fast",
                    "label": "MA fast",
                    "pane": "price",
                    "color": None,
                    "values": [None, 128501.0, 128502.0, 128503.0, 128504.0],
                }
            ],
        },
        "provenance": {
            "script": "research/scripts/run_backtest.py",
            "strategy_class": "my_strategies.MaCross",
            "git_revision": "abc123def456",
            "git_dirty": False,
        },
    }


def _import(body: dict, session) -> str:
    request = BacktestImportRequest.model_validate(body)
    return import_backtest(request, session=session)["run_id"]


def _lake_backtest_dirs(lake) -> list:
    backtests = lake / "backtests"
    return list(backtests.iterdir()) if backtests.exists() else []


def test_import_is_listed_detailed_and_served_by_read_endpoints(api_db_session, api_session_scope, lake):
    with api_session_scope() as session:
        run_id = _import(_body(), session)

    listing = list_backtests(session=api_db_session, limit=50, offset=0)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item.run_id == run_id
    assert item.origin == "script"
    assert item.status == "completed"
    assert item.symbol == "WIN$N"
    assert item.strategy == "ScriptStrategy"
    assert item.summary == {"total_pnl": 500.0, "trade_count": 1}

    detail = get_backtest(run_id, session=api_db_session)
    assert detail.origin == "script"
    assert detail.provenance is not None
    assert detail.provenance.model_dump(exclude_none=True) == _body()["provenance"]
    assert detail.config["engine"] == "candle"

    result = get_backtest_result(run_id)
    assert result["run_id"] == run_id
    assert result["metrics"] == {"total_pnl": 500.0, "trade_count": 1}
    assert [bar["timestamp"] for bar in result["bars"]] == [
        "2024-01-02T14:00:00Z",
        "2024-01-02T14:05:00Z",
        "2024-01-02T14:10:00Z",
        "2024-01-02T14:15:00Z",
        "2024-01-02T14:20:00Z",
    ]
    assert result["bars"][0]["open"] == 128500.0
    assert result["indicators"][0]["key"] == "ma_fast"
    assert result["indicators"][0]["values"] == [None, 128501.0, 128502.0, 128503.0, 128504.0]
    assert result["trades"][0]["id"] == "trade-1"


def test_import_serves_equity_trades_and_exports(api_db_session, api_session_scope, lake):
    with api_session_scope() as session:
        run_id = _import(_body(), session)

    equity = get_backtest_equity_artifact(run_id)
    assert [(point["time"], point["equity"]) for point in equity["points"]] == [
        ("2024-01-02T13:00:00Z", 100000.0),
        ("2024-01-02T14:10:00Z", 100500.0),
        ("2024-01-02T20:00:00Z", 100500.0),
    ]

    trades = get_backtest_trades_artifact(run_id)
    assert trades["trades"][0]["entry_time"] == "2024-01-02T14:00:00Z"
    assert trades["trades"][0]["exit_time"] == "2024-01-02T14:10:00Z"
    assert trades["trades"][0]["pnl"] == 500.0

    market_csv = export_backtest_market_data(run_id).body.decode()
    header = market_csv.splitlines()[0].split(",")
    assert header[0] == "time"
    assert "ma_fast" in header
    assert len(market_csv.splitlines()) == 1 + 5

    trades_csv = export_backtest_trades(run_id).body.decode()
    assert trades_csv.splitlines()[0].split(",")[0] == "trade_id"
    assert len(trades_csv.splitlines()) == 2


def _set_trade(body: dict, **changes) -> None:
    trade = body["result"]["trades"][0]
    for key, value in changes.items():
        if value is None:
            trade.pop(key, None)
        else:
            trade[key] = value


def _set_config(body: dict, **changes) -> None:
    body["config"].update(changes)


def _duplicate_bar_timestamp(body: dict) -> None:
    body["result"]["bars"][1]["timestamp"] = body["result"]["bars"][0]["timestamp"]


def _unordered_bars(body: dict) -> None:
    bars = body["result"]["bars"]
    bars[0], bars[1] = bars[1], bars[0]


def _duplicate_indicator_key(body: dict) -> None:
    body["result"]["indicators"].append(copy.deepcopy(body["result"]["indicators"][0]))


def _reserved_indicator_key(body: dict) -> None:
    body["result"]["indicators"][0]["key"] = "close"


VALIDATION_CASES = [
    (lambda b: b["result"].update(bars=[]), "must not be empty"),
    (_unordered_bars, "strictly ascending"),
    (_duplicate_bar_timestamp, "duplicate timestamp"),
    (lambda b: b["result"]["indicators"][0].update(values=[1.0]), "one value per bar"),
    (_duplicate_indicator_key, "duplicate indicator key"),
    (_reserved_indicator_key, "reserved"),
    (lambda b: _set_trade(b, status="OPEN"), "closed"),
    (lambda b: _set_trade(b, exit_time=None), "exit time"),
    (lambda b: _set_trade(b, pnl=None), "pnl"),
    (lambda b: _set_trade(b, entry_time="2024-01-02T08:00:00-03:00"), "outside the bar range"),
    (lambda b: _set_trade(b, exit_time="2024-01-02T18:00:00-03:00"), "outside the bar range"),
    (lambda b: _set_config(b, engine="tick"), "engine"),
    (lambda b: _set_config(b, ml_filter={"model_version_id": "a" * 64, "threshold": 0.5}), "ml_filter"),
    (lambda b: _set_config(b, entries=[{"strategy": "MACrossover", "params": {}}]), "entries"),
    (lambda b: _set_config(b, strategy=""), "strategy"),
]


@pytest.mark.parametrize(("mutate", "expected"), VALIDATION_CASES)
def test_import_rejects_invalid_request_before_writing(mutate, expected, api_db_session, api_session_scope, lake):
    body = _body()
    mutate(body)

    with pytest.raises(HTTPException) as raised:
        with api_session_scope() as session:
            _import(body, session)

    assert raised.value.status_code == 422
    assert expected in str(raised.value.detail)
    assert list_backtests(session=api_db_session, limit=50, offset=0)["total"] == 0
    assert _lake_backtest_dirs(lake) == []


def test_import_lake_write_failure_leaves_no_row_or_lake_directory(api_db_session, api_session_scope, lake):
    with patch("q_backend.backtesting.run_import.write_backtest_result", side_effect=OSError("disk full")):
        with pytest.raises(HTTPException) as raised:
            with api_session_scope() as session:
                _import(_body(), session)

    assert raised.value.status_code == 500
    assert list_backtests(session=api_db_session, limit=50, offset=0)["total"] == 0
    assert _lake_backtest_dirs(lake) == []
