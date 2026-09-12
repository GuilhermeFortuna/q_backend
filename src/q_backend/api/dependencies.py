from fastapi import HTTPException

from q_backend.api.deps import get_session
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import get_data_source

market_data_service = MarketDataService()


def get_market_data_service() -> MarketDataService:
    return market_data_service


def _require_mt5_live(service: MarketDataService | None = None) -> None:
    """Raise 503 when MT5 is required but unavailable; no-op when live MT5 is up."""
    service = service or market_data_service
    if service.mt5_available():
        return
    if get_data_source() == "mt5":
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")


def _data_source_payload(service: MarketDataService | None = None) -> dict:
    service = service or market_data_service
    return {
        "source": get_data_source(),
        # "Can we acquire fresh history?" — native MT5 or the remote gateway.
        # The Storage UI gates downloads on this, so native-only would wrongly
        # report offline on Linux even with a healthy gateway.
        "mt5_available": service.acquisition_available(),
        "active_provider": service.active_provider(),
    }


__all__ = [
    "get_session",
    "market_data_service",
    "get_market_data_service",
    "_require_mt5_live",
    "_data_source_payload",
]
