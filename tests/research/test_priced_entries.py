"""Market entry regression and shared tick fixtures."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY = date(2026, 10, 5)


def _bar(clock: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY.isoformat()} {clock}", tz=BRASILIA_TZ)


def _write_session(root, rows, day=DAY, symbol=SYMBOL):
    times = []
    for clock, _ in rows:
        hour, minute, second = (int(part) for part in clock.split(":"))
        moment = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=timezone.utc)
        times.append(int(moment.timestamp() * 1000))
    count = len(rows)
    prices = np.asarray([price for _, price in rows], dtype=np.float64)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": prices,
        "ask": prices,
        "last": prices,
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.full(count, 32, dtype=np.int32),
    }
    directory = root / _slug_symbol(symbol)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)
    return TickStore(symbol, root=root)


def _m10_frame(store: TickStore) -> pd.DataFrame:
    return store.bars("M10", start=DAY.isoformat())


class Scripted(ResearchStrategy):
    def __init__(self, orders: dict[int, TradeOrder]) -> None:
        self.orders = orders

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        return self.orders.get(len(frame) - 1)


# Criterion 6: Unpriced order run is identical to today's output
def test_unpriced_orders_unaffected(tmp_path) -> None:
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 101.0),
        ("10:15:00", 102.0),
        ("10:20:00", 103.0),
    ]
    store = _write_session(tmp_path / "ticks", rows)
    frame = _m10_frame(store)

    # Unpriced buy on bar 0 fills at bar 1 open (101.0)
    result = backtest(frame, strategy=Scripted({0: TradeOrder.buy()}), symbol=SYMBOL)
    trades = result.trades
    assert len(trades) == 1
    assert trades["entry_time"].tolist() == [_bar("10:10")]
    assert trades["entry_price"].tolist() == [101.0]
