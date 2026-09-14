from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from q_backend.market_data.clients.metatrader import MetaTraderClient


def __getattr__(name: str):
    if name == "MetaTraderClient":
        from q_backend.market_data.clients.metatrader import MetaTraderClient

        return MetaTraderClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MetaTraderClient"]
