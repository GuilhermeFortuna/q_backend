import os
import logging
from datetime import datetime
from typing import List, Literal, Optional
from pathlib import Path

import numpy as np

from q_backend.market_data.clients.local import LocalParquetClient
from q_backend.market_data.clients.metatrader import (
    MetaTraderClient,
    OhlcvAvailableRange,
)
from q_backend.market_data.models import OHLCV, Tick
from q_backend.storage.runtime_config import get_data_source

logger = logging.getLogger(__name__)


def load_env():
    """
    Manually load environment variables from the root .env file.
    Done manually to avoid adding extra dependencies for config.
    """
    # service.py is at: src/q_backend/market_data/service.py
    # Project root is 3 parent folders up: src/q_backend/
    root_path = Path(__file__).resolve().parents[3]
    env_path = root_path / ".env"
    if env_path.exists():
        logger.info(f"Loading environment variables from: {env_path}")
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Remove inline comments (e.g. key=val # comment)
                if " #" in line:
                    line = line.split(" #", 1)[0].strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    # Clean potential quotes around values
                    val = val.strip().strip('"').strip("'")
                    os.environ[key.strip()] = val
    else:
        logger.warning(f".env file not found at: {env_path}")


class MarketDataService:
    """
    High-level service managing market data connections and routing requests
    to the active provider (MetaTrader 5 or local parquet).
    """

    def __init__(self):
        load_env()

        login_str = os.getenv("MT5_USER")
        login = int(login_str) if login_str and login_str.strip().isdigit() else None

        # Clean up password if there is an inline comment suffix that wasn't stripped
        password = os.getenv("MT5_PASSWORD")
        if password and " #" in password:
            password = password.split(" #")[0].strip()

        server = os.getenv("MT5_SERVER")
        path = os.getenv("MT5_PATH")

        logger.info(
            f"Initializing MarketDataService with MT5 User: {login}, Server: {server}"
        )

        self.mt5_client = MetaTraderClient(
            path=path, login=login, password=password, server=server
        )
        self._local_client = LocalParquetClient()

    def _resolve_provider(self):
        source = get_data_source()
        if source == "local":
            return self._local_client
        if source == "mt5":
            if not self.mt5_client.is_supported():
                raise ConnectionError(
                    "data_source is 'mt5' but MetaTrader5 is not installed on this "
                    "platform. Install the Windows MetaTrader5 package or switch to "
                    "'auto'/'local'."
                )
            # The terminal may still be disconnected — let the MT5 call surface that
            # as a ConnectionError rather than silently serving local data.
            return self.mt5_client
        # auto: prefer MT5 wherever this platform can run it, so a transient terminal
        # outage surfaces as an error from the MT5 call instead of a quiet fall back to
        # (possibly empty) local data. Use local only when MT5 cannot run here at all.
        if self.mt5_client.is_supported():
            return self.mt5_client
        return self._local_client

    def active_provider(self) -> Literal["mt5", "local"]:
        # Mirror _resolve_provider's intent without raising (callers like the
        # data-source endpoint must not 500 on an explicit-'mt5' misconfig).
        source = get_data_source()
        if source == "local":
            return "local"
        if source == "mt5":
            return "mt5"
        return "mt5" if self.mt5_client.is_supported() else "local"

    def mt5_available(self) -> bool:
        return self.mt5_client.is_available()

    def mt5_connected(self) -> bool:
        """Whether MT5 is connected right now (does not attempt reconnect)."""
        if not self.mt5_client.is_supported():
            return False
        return self.mt5_client._is_initialized

    def is_available(self) -> bool:
        try:
            self._resolve_provider()
            return True
        except ConnectionError:
            return False

    def initialize(self) -> bool:
        """
        Best-effort MT5 connect on startup. Failure does not crash the API;
        auto mode falls back to the local provider.
        """
        logger.info("Initializing MetaTrader client connection (best-effort)...")
        try:
            return self.mt5_client.connect()
        except Exception as exc:
            logger.warning("MetaTrader connect failed on startup: %s", exc)
            return False

    def shutdown(self) -> None:
        """
        Gracefully disconnects all market data clients.
        """
        logger.info("Shutting down market data clients...")
        self.mt5_client.disconnect()

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        return self._resolve_provider().get_symbol_info(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> List[OHLCV]:
        return self._resolve_provider().get_ohlcv(symbol, timeframe, start, end)

    def get_available_ohlcv_range(
        self, symbol: str, timeframe: str
    ) -> Optional[OhlcvAvailableRange]:
        return self._resolve_provider().get_available_ohlcv_range(symbol, timeframe)

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> List[Tick]:
        return self._resolve_provider().get_ticks(symbol, start, end)

    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]:
        return self._resolve_provider().get_ticks_columnar(
            symbol, start, end, flags=flags, use_cache=use_cache
        )

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> List[Tick]:
        return self._resolve_provider().get_recent_ticks(symbol, limit)

    def search_symbols(self, query: str) -> list:
        return self._resolve_provider().search_symbols(query)
