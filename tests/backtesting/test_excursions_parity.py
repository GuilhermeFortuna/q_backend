from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import random

import numpy as np
import pandas as pd

from q_backend.backtesting import excursions as actual
from q_backend.backtesting.models import OrderAction, Trade
from tests.backtesting import excursions_reference as reference


def _fixture():
    rng = random.Random(131)
    start = pd.Timestamp("2026-03-01", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": np.full(24, 100.0),
            "high": [100 + rng.randrange(1, 8) for _ in range(24)],
            "low": [100 - rng.randrange(1, 8) for _ in range(24)],
            "close": [100 + rng.randrange(-3, 4) for _ in range(24)],
        },
        index=pd.date_range(start, periods=24, freq="min"),
    )
    deal_bars = {}
    for index, resolution in enumerate(("tick", "minute", "candle")):
        for bar in range(24):
            if (bar + index) % 7 == 0:
                deal_bars[(index, bar)] = None
                continue
            stamp = bars.index[bar]
            deal_bars[(index, bar)] = actual.DealBar(
                times=[stamp + pd.Timedelta(seconds=10 * n) for n in range(6)],
                # Repeated extremes exercise first-occurrence tie behavior.
                prices=[100.0, 107.0, 93.0, 107.0, 93.0, 100.0],
                resolution=resolution,
            )

    trades = []
    fill_kinds = ("market", "priced", "stop", "limit")
    for number in range(600):
        entry_bar = rng.randrange(20)
        exit_bar = min(23, entry_bar + rng.randrange(0, 4))
        intrabar_entry = number % 2 == 0
        intrabar_exit = number % 3 != 0
        entry_time = bars.index[entry_bar].to_pydatetime() + timedelta(seconds=5 if intrabar_entry else 0)
        exit_time = bars.index[exit_bar].to_pydatetime() + timedelta(seconds=45 if intrabar_exit else 0)
        action = OrderAction.BUY if number % 2 == 0 else OrderAction.SELL
        entry_price = 100.0 + rng.randrange(-2, 3)
        trade = Trade(
            id=f"t{number}",
            order_id=f"o{number}",
            symbol="TEST",
            action=action,
            quantity=1 + number % 3,
            point_value=2.5,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=entry_price + (1 if action == OrderAction.BUY else -1),
            pnl=1.0,
            status="CLOSED",
            entry_bar=entry_bar,
            exit_bar=exit_bar,
            order_kind=fill_kinds[number % len(fill_kinds)],
            entry_tick_time=entry_time if intrabar_entry else None,
            exit_tick_time=exit_time if intrabar_exit else None,
        )
        trades.append(trade)
    return bars, deal_bars, trades


def _fields(trade):
    return {
        name: getattr(trade, name)
        for name in (
            "mfe_points",
            "mae_points",
            "mfe_time",
            "mae_time",
            "mfe_capture",
            "post_exit_mfe_points",
            "post_exit_mae_points",
            "post_exit_return_points",
            "post_exit_bars_observed",
            "exit_bar_rest_high",
            "exit_bar_rest_low",
            "excursion_resolution",
        )
    }


def test_measurement_matches_q130_reference_for_seeded_trade_matrix():
    bars, deal_bars, trades = _fixture()
    expected, observed = deepcopy(trades), deepcopy(trades)
    actual_deals = lambda bar: deal_bars[(bar % 3, bar)]
    reference_deals = lambda bar: deal_bars[(bar % 3, bar)]

    reference.measure_trades(expected, bars, deals=reference_deals, post_exit_bars=7, day_trade_close="00:20")
    actual.measure_trades(observed, bars, deals=actual_deals, post_exit_bars=7, day_trade_close="00:20")

    assert len(observed) >= 500
    assert [_fields(trade) for trade in observed] == [_fields(trade) for trade in expected]
    expected_without_source, observed_without_source = deepcopy(trades), deepcopy(trades)
    reference.measure_trades(expected_without_source, bars, post_exit_bars=3)
    actual.measure_trades(observed_without_source, bars, post_exit_bars=3)
    assert [_fields(trade) for trade in observed_without_source] == [
        _fields(trade) for trade in expected_without_source
    ]
    for window in (1, 4, 10):
        expected_post = reference.post_exit_from_stored(
            expected, bars, post_exit_bars=window, day_trade_close=time(0, 20)
        )
        actual_post = actual.post_exit_from_stored(observed, bars, post_exit_bars=window, day_trade_close=time(0, 20))
        assert actual_post == expected_post


def test_measurement_preserves_non_utc_timezones():
    bars, deal_bars, trades = _fixture()
    bars.index = bars.index.tz_convert("America/Sao_Paulo")
    localized_deals = {
        key: (
            None
            if value is None
            else actual.DealBar(
                times=pd.DatetimeIndex(value.times).tz_convert("America/Sao_Paulo"),
                prices=value.prices,
                resolution=value.resolution,
            )
        )
        for key, value in deal_bars.items()
    }
    for trade in trades:
        trade.entry_time = pd.Timestamp(trade.entry_time).tz_convert("America/Sao_Paulo").to_pydatetime()
        trade.exit_time = pd.Timestamp(trade.exit_time).tz_convert("America/Sao_Paulo").to_pydatetime()
        if trade.entry_tick_time:
            trade.entry_tick_time = pd.Timestamp(trade.entry_tick_time).tz_convert("America/Sao_Paulo").to_pydatetime()
        if trade.exit_tick_time:
            trade.exit_tick_time = pd.Timestamp(trade.exit_tick_time).tz_convert("America/Sao_Paulo").to_pydatetime()

    expected, observed = deepcopy(trades), deepcopy(trades)
    reference.measure_trades(expected, bars, deals=lambda bar: localized_deals[(bar % 3, bar)], post_exit_bars=4)
    actual.measure_trades(observed, bars, deals=lambda bar: localized_deals[(bar % 3, bar)], post_exit_bars=4)
    assert [_fields(trade) for trade in observed] == [_fields(trade) for trade in expected]
    assert [str(trade.mfe_time.tzinfo) for trade in observed] == [str(trade.mfe_time.tzinfo) for trade in expected]
    assert [str(trade.mae_time.tzinfo) for trade in observed] == [str(trade.mae_time.tzinfo) for trade in expected]
