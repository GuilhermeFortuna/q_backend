import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import OhlcvAvailableRange
from q_backend.market_data.columnar import columnar_to_ticks
from q_backend.market_data.models import OHLCV, Tick

logger = logging.getLogger(__name__)


class LocalParquetClient:
    """Local parquet market-data provider."""

    def is_available(self) -> bool:
        return True

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]:
        return local_store.read_ohlcv(symbol, timeframe, start, end)

    def get_available_ohlcv_range(self, symbol: str, timeframe: str) -> Optional[OhlcvAvailableRange]:
        return local_store.available_range(symbol, timeframe)

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]:
        arrays = local_store.read_ticks_columnar(symbol, start, end)
        return columnar_to_ticks(arrays)

    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]:
        if flags is not None:
            logger.debug(
                "LocalParquetClient serves all stored ticks for %s " "(requested flags=%s ignored in local mode)",
                symbol,
                flags,
            )
        return local_store.read_ticks_columnar(symbol, start, end)

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> list[Tick]:
        entry = local_store.tick_available_range(symbol)
        if entry is None:
            return []
        start = datetime.fromisoformat(entry["start"])
        end = datetime.fromisoformat(entry["end"])
        ticks = self.get_ticks(symbol, start, end)
        return ticks[-limit:]

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        needle = query.strip().upper()
        if not needle:
            return []
        return [entry for entry in local_store.stored_symbols() if needle in entry["name"].upper()]

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        symbol = symbol.upper()
        for entry in local_store.stored_symbols():
            if entry["name"].upper() == symbol:
                return {
                    "name": entry["name"],
                    "description": entry["description"],
                    "path": entry["path"],
                    "custom": entry["custom"],
                }
        return None
