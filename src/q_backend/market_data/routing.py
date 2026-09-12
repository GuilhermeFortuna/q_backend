"""Per-symbol OHLCV provider routing (local parquet vs native MT5 vs remote gateway).

`auto` preference order (WO185): **native MT5 → remote gateway → local parquet**.
Native MT5 wins when the terminal is connected and the symbol is selectable; otherwise
the remote gateway wins when it is reachable; otherwise local parquet when data exists.
A symbol with no local data and no reachable provider resolves to the best available
acquirer so the downstream error is honest rather than a silent empty result.

When `auto` resolves to the remote gateway, ``MarketDataService.get_ohlcv`` applies
coverage planning (WO188): local parquet is the fast path when its envelope already
covers the requested range; otherwise only missing head/tail segments are fetched
from the gateway, persisted via fetch-through, and the full range is served from
local parquet. Explicit ``remote`` source always performs a full-range gateway fetch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from q_backend.market_data import local_store
from q_backend.storage.runtime_config import get_data_source

if TYPE_CHECKING:
    from q_backend.market_data.service import MarketDataService


def has_local_ohlcv(symbol: str, timeframe: str) -> bool:
    return local_store.available_range(symbol, timeframe) is not None


def symbol_selectable_in_mt5(service: MarketDataService, symbol: str) -> bool:
    if not service.mt5_available():
        return False
    from q_backend.market_data.clients import metatrader as mt5_client_module

    mt5 = mt5_client_module.mt5
    if mt5 is None:
        return False
    return bool(mt5.symbol_select(symbol, True))


def resolve_ohlcv_source(service: MarketDataService, symbol: str, timeframe: str) -> Literal["local", "mt5", "remote"]:
    """Pick the OHLCV provider for a symbol/timeframe pair."""
    source = get_data_source()
    if source == "local":
        return "local"
    if source == "mt5":
        return "mt5"
    if source == "remote":
        return "remote"

    # auto: native MT5 → remote gateway → local parquet.
    if symbol_selectable_in_mt5(service, symbol):
        return "mt5"
    if service._remote_client.is_available():
        return "remote"
    if has_local_ohlcv(symbol, timeframe):
        return "local"
    # No local data and no reachable provider: fall back to the native acquirer so
    # the caller gets an honest ConnectionError/empty-history error (today's shape).
    return "mt5"
