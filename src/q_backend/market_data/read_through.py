"""Read-through OHLCV loader for research pipelines (WO189)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV

if TYPE_CHECKING:
    from q_backend.market_data.service import MarketDataService

logger = logging.getLogger(__name__)


def _resolve_service(
    explicit: MarketDataService | None,
) -> MarketDataService:
    if explicit is not None:
        return explicit
    from q_backend.tasks.worker_context import peek_worker_market_data_service

    worker_service = peek_worker_market_data_service()
    if worker_service is not None:
        return worker_service
    from q_backend.api.dependencies import market_data_service

    return market_data_service


def read_ohlcv_fresh(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    service: MarketDataService | None = None,
) -> list[OHLCV]:
    """``local_store.read_ohlcv`` drop-in that routes through ``MarketDataService``.

    Service resolution when ``service`` is omitted:

    1. Initialized Dramatiq worker ``MarketDataService`` (``peek_worker_market_data_service``
       only — never triggers lazy creation).
    2. API-process ``dependencies.market_data_service`` singleton.

    Coverage gaps are gap-filled from the gateway when routing selects ``remote`` in
    ``auto`` mode (WO188). With no gateway and no native MT5, behavior matches
    ``local_store.read_ohlcv``.

    On ``ConnectionError`` (for example explicit ``remote`` with the gateway down),
    falls back to ``local_store.read_ohlcv`` with a warning so research pipelines stay
    runnable offline.
    """
    svc = _resolve_service(service)
    try:
        return svc.get_ohlcv(symbol, timeframe, start, end)
    except ConnectionError:
        logger.warning(
            "read_ohlcv_fresh: gateway unreachable for %s/%s [%s, %s]; "
            "falling back to local parquet",
            symbol,
            timeframe,
            start,
            end,
        )
        return local_store.read_ohlcv(symbol, timeframe, start, end)
