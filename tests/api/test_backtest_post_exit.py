from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException

from q_backend.api.routers.backtest import get_backtest_post_exit
from q_backend.backtesting import run_service


def _trade_record():
    return {
        "id": "trade-1",
        "order_id": "order-1",
        "symbol": "WIN$",
        "action": "BUY",
        "quantity": 2,
        "entry_time": "2024-01-01T00:00:00+00:00",
        "entry_price": 100,
        "exit_time": "2024-01-01T00:01:00+00:00",
        "exit_price": 102,
        "status": "CLOSED",
        "pnl": 4,
        "point_value": 1,
        "exit_bar_rest_high": 103,
        "exit_bar_rest_low": 101,
    }


def test_post_exit_endpoint_recalculates_from_stored_bars_only():
    trade_frame = pd.DataFrame([_trade_record()])
    market_frame = pd.DataFrame(
        {
            "time": ["2024-01-01T00:00:00Z", "2024-01-01T00:01:00Z", "2024-01-01T00:02:00Z"],
            "open": [100, 102, 104],
            "high": [103, 105, 106],
            "low": [99, 101, 103],
            "close": [102, 104, 105],
        }
    )
    with (
        patch.object(
            run_service,
            "_read_export_artifact",
            side_effect=lambda _id, kind: {
                "trades": trade_frame,
                "market_data": market_frame,
            }[kind],
        ),
        patch.object(run_service, "_stored_run_config", return_value={"day_trade": False}),
    ):
        result = get_backtest_post_exit("run-1", bars=1)

    assert result.post_exit_bars == 1
    assert result.trades[0].post_exit_mfe_points == 4
    assert result.trades[0].post_exit_mae_points == 1
    assert result.trades[0].post_exit_return_points == 3
    assert result.trades[0].post_exit_bars_observed == 1
    assert result.all.avg_post_exit_return == 6


def test_post_exit_endpoint_rejects_invalid_bar_count():
    with pytest.raises(HTTPException) as error:
        run_service._replace_post_exit_values("run-1", [_trade_record()], 0)
    assert error.value.status_code == 422
