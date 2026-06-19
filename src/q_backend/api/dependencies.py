from fastapi import HTTPException

from q_backend.api.deps import get_session
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import get_data_source

market_data_service = MarketDataService()


def get_market_data_service() -> MarketDataService:
    return market_data_service


def _require_mt5_live() -> None:
    """Raise 503 when MT5 is required but unavailable; no-op when live MT5 is up."""
    if market_data_service.mt5_available():
        return
    if get_data_source() == "mt5":
        raise HTTPException(
            status_code=503, detail="MetaTrader 5 terminal is offline."
        )


def _data_source_payload() -> dict:
    return {
        "source": get_data_source(),
        "mt5_available": market_data_service.mt5_connected(),
        "active_provider": market_data_service.active_provider(),
    }


__all__ = [
    "get_session",
    "market_data_service",
    "get_market_data_service",
    "_require_mt5_live",
    "_data_source_payload",
]
