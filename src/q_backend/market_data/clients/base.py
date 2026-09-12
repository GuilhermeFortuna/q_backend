from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np

from q_backend.market_data.clients.metatrader import OhlcvAvailableRange
from q_backend.market_data.models import OHLCV, Tick


@runtime_checkable
class MarketDataProvider(Protocol):
    def is_available(self) -> bool: ...

    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]: ...

    def get_available_ohlcv_range(self, symbol: str, timeframe: str) -> Optional[OhlcvAvailableRange]: ...

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]: ...

    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]: ...

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> list[Tick]: ...

    def search_symbols(self, query: str) -> list[dict[str, Any]]: ...

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None: ...
