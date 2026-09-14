from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from q_backend.market_data.service import MarketDataService


def __getattr__(name: str):
    if name == "MarketDataService":
        from q_backend.market_data.service import MarketDataService

        return MarketDataService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MarketDataService"]
