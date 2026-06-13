"""Minimal MetaTrader5 stub so the API and worker start in Linux Docker.

Live market data requires the real Windows MetaTrader5 package and terminal.
Backtests, optimizations, and other jobs that use cached or uploaded data still
work when MT5 initialization fails at startup.
"""

from __future__ import annotations

from typing import Any

TIMEFRAME_M1 = 1
TIMEFRAME_M2 = 2
TIMEFRAME_M3 = 3
TIMEFRAME_M4 = 4
TIMEFRAME_M5 = 5
TIMEFRAME_M6 = 6
TIMEFRAME_M10 = 10
TIMEFRAME_M12 = 12
TIMEFRAME_M15 = 15
TIMEFRAME_M20 = 20
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H2 = 16386
TIMEFRAME_H3 = 16387
TIMEFRAME_H4 = 16388
TIMEFRAME_H6 = 16390
TIMEFRAME_H8 = 16392
TIMEFRAME_H12 = 16396
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
TIMEFRAME_MN1 = 49153

COPY_TICKS_ALL = 1
COPY_TICKS_TRADE = 2

TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64

_STUB_ERROR = (
    -1,
    "MetaTrader5 is unavailable in Docker (Windows-only). Run the backend natively for live MT5 data.",
)


def initialize(**_kwargs: Any) -> bool:
    return False


def shutdown() -> None:
    return None


def login(**_kwargs: Any) -> bool:
    return False


def last_error() -> tuple[int, str]:
    return _STUB_ERROR


def symbol_select(_symbol: str, _enable: bool) -> bool:
    return False


def symbol_info(_symbol: str) -> None:
    return None


def symbol_info_tick(_symbol: str) -> None:
    return None


def copy_rates_range(*_args: Any, **_kwargs: Any) -> None:
    return None


def copy_rates_from_pos(*_args: Any, **_kwargs: Any) -> None:
    return None


def copy_rates_from(*_args: Any, **_kwargs: Any) -> None:
    return None


def copy_ticks_range(*_args: Any, **_kwargs: Any) -> None:
    return None


def symbols_get(_pattern: str | None = None) -> list[Any]:
    return []
