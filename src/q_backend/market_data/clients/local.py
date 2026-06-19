import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

from q_backend.market_data.clients.metatrader import OhlcvAvailableRange, _empty_ticks_columnar
from q_backend.market_data.models import OHLCV, Tick

logger = logging.getLogger(__name__)


class LocalParquetClient:
    """Local parquet market-data provider (stub until WO48 fills the store)."""

    def is_available(self) -> bool:
        return True

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def get_ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[OHLCV]:
        return []

    def get_available_ohlcv_range(
        self, symbol: str, timeframe: str
    ) -> Optional[OhlcvAvailableRange]:
        return None

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]:
        return []

    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]:
        return _empty_ticks_columnar()

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> list[Tick]:
        return []

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        return []

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        return None
