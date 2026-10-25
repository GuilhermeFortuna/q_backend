from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from q_backend.backtesting.excursions import DealBar, measure_trades
from q_backend.backtesting.models import OrderAction, Trade


def _trade(side=OrderAction.BUY, *, exit_tick=False):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=side,
        quantity=1,
        entry_time=start,
        entry_price=100,
        exit_time=start + timedelta(minutes=2),
        exit_price=102 if side == OrderAction.BUY else 98,
        pnl=2,
        status="CLOSED",
        entry_bar=0,
        exit_bar=2,
        exit_tick_time=(start + timedelta(minutes=2, seconds=30)) if exit_tick else None,
    )


def test_measure_trades_uses_open_bar_path_and_post_exit_window():
    start = pd.Timestamp("2024-01-01", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100, 101, 101, 104],
            "high": [103, 105, 104, 106],
            "low": [99, 100, 98, 103],
            "close": [101, 102, 102, 105],
        },
        index=pd.date_range(start, periods=4, freq="min"),
    )
    trade = _trade()
    trade.quantity = 2
    trade.point_value = 10
    measure_trades([trade], bars, post_exit_bars=1)

    assert trade.mfe_points == 5
    assert trade.mae_points == 2
    assert trade.mfe_capture == pytest.approx(40 / 100)
    assert trade.post_exit_mfe_points == 4
    assert trade.post_exit_mae_points == 4
    assert trade.post_exit_return_points == 3
    assert trade.post_exit_bars_observed == 1
    assert trade.excursion_resolution == "tick"


def test_measure_trades_uses_only_deals_between_partial_fill_moments():
    start = pd.Timestamp("2024-01-01", tz="UTC")
    bars = pd.DataFrame(
        {"open": [100], "high": [120], "low": [80], "close": [101]},
        index=pd.DatetimeIndex([start]),
    )
    trade = _trade()
    trade.exit_bar = 0
    trade.exit_time = start.to_pydatetime() + timedelta(seconds=30)
    trade.exit_tick_time = trade.exit_time
    trade.exit_price = 101
    deals = DealBar(
        times=[
            start - pd.Timedelta(seconds=2),
            start + pd.Timedelta(seconds=10),
            start + pd.Timedelta(seconds=30),
            start + pd.Timedelta(seconds=40),
        ],
        prices=[120, 105, 101, 80],
    )
    measure_trades([trade], bars, deals=lambda _: deals, post_exit_bars=1)

    assert trade.mfe_points == 5
    assert trade.mae_points == 0
    assert trade.exit_bar_rest_high == 101
    assert trade.exit_bar_rest_low == 80
