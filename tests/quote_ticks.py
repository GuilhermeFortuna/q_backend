"""Write synthetic quote ticks into a tick store laid out like the real one."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import numpy as np

from q_backend.research.tick_store import _slug_symbol, _write_day_atomic, naive_local_to_time_msc

QUOTE_FLAGS = 6  # MQL5 bid | ask
TRADE_FLAGS = 8  # MQL5 last only
BUY_FLAG = 32
SELL_FLAG = 64


def write_quote_ticks(
    root: Path,
    symbol: str,
    ticks: Iterable[tuple[datetime, float, float] | tuple[datetime, float, float, int]],
) -> None:
    """Store ``(naive Brasília time, bid, ask[, flags])`` ticks, one file per calendar day.

    Times use the MT5 convention the store holds: the UTC wall clock of ``time_msc`` reads
    as Brasília local time.
    """
    days: dict = defaultdict(lambda: {"time_msc": [], "bid": [], "ask": [], "flags": []})
    for moment, bid, ask, *flags in ticks:
        day = days[moment.date()]
        day["time_msc"].append(naive_local_to_time_msc(moment))
        day["bid"].append(bid)
        day["ask"].append(ask)
        day["flags"].append(flags[0] if flags else QUOTE_FLAGS)

    directory = root / _slug_symbol(symbol)
    for day, columns in days.items():
        count = len(columns["time_msc"])
        mid = (np.asarray(columns["bid"]) + np.asarray(columns["ask"])) / 2.0
        _write_day_atomic(
            directory / f"{day.isoformat()}.parquet",
            {
                "time_msc": np.asarray(columns["time_msc"], dtype=np.int64),
                "bid": np.asarray(columns["bid"], dtype=np.float64),
                "ask": np.asarray(columns["ask"], dtype=np.float64),
                "last": mid,
                "volume": np.ones(count, dtype=np.float64),
                "flags": np.asarray(columns["flags"], dtype=np.int32),
            },
        )


def write_trade_ticks(
    root: Path,
    symbol: str,
    ticks: Iterable[tuple[datetime, float, float, int]],
) -> None:
    """Store ``(naive Brasília time, last, volume, flags)`` trade ticks per calendar day."""
    days: dict = defaultdict(lambda: {"time_msc": [], "last": [], "volume": [], "flags": []})
    for moment, last, volume, flags in ticks:
        day = days[moment.date()]
        day["time_msc"].append(naive_local_to_time_msc(moment))
        day["last"].append(last)
        day["volume"].append(volume)
        day["flags"].append(flags)

    directory = root / _slug_symbol(symbol)
    for day, columns in days.items():
        count = len(columns["time_msc"])
        bid = np.asarray(columns["last"], dtype=np.float64) - 0.05
        ask = np.asarray(columns["last"], dtype=np.float64) + 0.05
        _write_day_atomic(
            directory / f"{day.isoformat()}.parquet",
            {
                "time_msc": np.asarray(columns["time_msc"], dtype=np.int64),
                "bid": bid,
                "ask": ask,
                "last": np.asarray(columns["last"], dtype=np.float64),
                "volume": np.asarray(columns["volume"], dtype=np.float64),
                "flags": np.asarray(columns["flags"], dtype=np.int32),
            },
        )
