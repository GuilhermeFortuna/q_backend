"""Remote-safe types and constants shared by market-data clients.

This module deliberately has no MetaTrader5 dependency: the Linux gateway client
must be importable in a process that never loads the terminal package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from q_backend.market_data.timezone import unix_seconds_to_brasilia_naive

# Values provided by the Linux MT5 stub. The Windows SDK's COPY_TICKS_ALL value
# differs; preserving that mismatch is outside this module's compatibility role.
COPY_TICKS_ALL = 1
COPY_TICKS_TRADE = 2


@dataclass(frozen=True)
class OhlcvAvailableRange:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    bar_count: int


TRADE_COLUMNS = ("time_msc", "price", "volume", "volume_real", "raw_flags", "occurrence")


@dataclass(frozen=True)
class TradeRange:
    """Eligible trades for one half-open UTC range, with the gateway's coverage facts.

    ``columns`` holds the arrays named in ``TRADE_COLUMNS``; ``volume_real`` is NaN where
    the provider offers none. ``volume_field``/``volume_unit`` say which raw volume
    column carries analysis volume for this response. A range that is not
    ``range_complete`` is never presented as complete: ``coverage_reason`` says why.
    """

    columns: Mapping[str, np.ndarray]
    provider_id: str
    symbol: str
    source_generation: str
    volume_field: str
    volume_unit: str
    availability: str
    range_complete: bool
    covered_from_utc: datetime | None
    covered_to_utc: datetime | None
    coverage_reason: str | None
    invalid_trade_count: int
    truncated: bool

    def __len__(self) -> int:
        return len(self.columns["time_msc"])


_RECENT_TICKS_WINDOWS = (
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
)


def _empty_ticks_columnar() -> dict[str, np.ndarray]:
    return {
        "time_msc": np.array([], dtype=np.int64),
        "bid": np.array([], dtype=np.float64),
        "ask": np.array([], dtype=np.float64),
        "last": np.array([], dtype=np.float64),
        "volume": np.array([], dtype=np.float64),
        "flags": np.array([], dtype=np.int32),
    }


def _time_msc_to_naive_local(msc: int) -> datetime:
    sec = msc // 1000
    ms_remainder = msc % 1000
    return unix_seconds_to_brasilia_naive(sec) + timedelta(milliseconds=ms_remainder)
