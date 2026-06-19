"""Per-symbol OHLCV provider routing (local parquet vs MT5)."""

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


def resolve_ohlcv_source(
    service: MarketDataService, symbol: str, timeframe: str
) -> Literal["local", "mt5"]:
    """Pick the OHLCV provider for a symbol/timeframe pair."""
    source = get_data_source()
    if source == "local":
        return "local"
    if source == "mt5":
        return "mt5"
    if not has_local_ohlcv(symbol, timeframe):
        return "mt5"
    if not service.mt5_available():
        return "local"
    if not symbol_selectable_in_mt5(service, symbol):
        return "local"
    return "mt5"
