from datetime import datetime, timezone

import pandas as pd
import pytest

from q_backend.storage.lake.artifacts import (
    delete_backtest_artifacts,
    lake_root,
    read_backtest_artifact,
    write_backtest_artifacts,
)
from q_backend.storage.settings import get_settings


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_write_and_read_backtest_artifacts_round_trip(lake_root_path):
    run_id = "11111111-1111-1111-1111-111111111111"
    trades = pd.DataFrame(
        [
            {
                "id": "t1",
                "symbol": "WIN$",
                "action": "BUY",
                "quantity": 1.0,
                "entry_time": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "entry_price": 100.0,
                "exit_time": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "exit_price": 101.0,
                "status": "CLOSED",
                "pnl": 10.0,
            }
        ]
    )
    equity = pd.DataFrame(
        {
            "time": [
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 3, tzinfo=timezone.utc),
            ],
            "equity": [100000.0, 100010.0],
        }
    )

    paths = write_backtest_artifacts(run_id, trades, equity)

    assert paths == {
        "trades": f"backtests/{run_id}/trades.parquet",
        "equity": f"backtests/{run_id}/equity.parquet",
    }
    assert (lake_root_path / paths["trades"]).is_file()
    assert (lake_root_path / paths["equity"]).is_file()

    read_trades = read_backtest_artifact(run_id, "trades")
    read_equity = read_backtest_artifact(run_id, "equity")

    pd.testing.assert_frame_equal(
        read_trades.reset_index(drop=True),
        trades.reset_index(drop=True),
        check_dtype=False,
    )
    pd.testing.assert_frame_equal(
        read_equity.reset_index(drop=True),
        equity.reset_index(drop=True),
        check_dtype=False,
    )


def test_read_backtest_artifact_raises_when_missing(lake_root_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        read_backtest_artifact("00000000-0000-0000-0000-000000000000", "equity")


def test_delete_backtest_artifacts_removes_run_directory(lake_root_path):
    run_id = "22222222-2222-2222-2222-222222222222"
    trades = pd.DataFrame([{"id": "t1", "pnl": 1.0}])
    equity = pd.DataFrame({"time": [datetime(2024, 1, 1)], "equity": [1.0]})

    write_backtest_artifacts(run_id, trades, equity)
    assert (lake_root() / "backtests" / run_id).is_dir()

    delete_backtest_artifacts(run_id)

    assert not (lake_root() / "backtests" / run_id).exists()
