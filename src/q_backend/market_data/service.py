import os
import logging
from datetime import datetime
from typing import List, Literal, Optional, Union
from pathlib import Path

import numpy as np

from q_backend.market_data import local_store
from q_backend.market_data.clients.local import LocalParquetClient
from q_backend.market_data.clients.metatrader import (
    MetaTraderClient,
    OhlcvAvailableRange,
)
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.models import OHLCV, Tick
from q_backend.market_data.routing import resolve_ohlcv_source
from q_backend.storage.runtime_config import get_data_source

logger = logging.getLogger(__name__)

_REMOTE_NOT_CONFIGURED = (
    "data_source is 'remote' but no gateway URL is configured. Set "
    "Q_MT5_GATEWAY_URL (or the 'remote_gateway_url' runtime-config key) or switch "
    "to 'auto'/'local'."
)


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
        self._remote_client = RemoteMt5Client()
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
        if source == "remote":
            if not self._remote_client.is_supported():
                raise ConnectionError(_REMOTE_NOT_CONFIGURED)
            # Configured but possibly unhealthy — let the call surface the
            # ConnectionError rather than silently serving local data.
            return self._remote_client
        # auto: native MT5 → remote gateway → local parquet.
        if self.mt5_client.is_supported() and self.mt5_available():
            return self.mt5_client
        if self._remote_client.is_available():
            return self._remote_client
        return self._local_client

    def active_provider(self) -> Literal["mt5", "remote", "local"]:
        # Mirror _resolve_provider's intent without raising (callers like the
        # data-source endpoint must not 500 on an explicit misconfig).
        source = get_data_source()
        if source == "local":
            return "local"
        if source == "mt5":
            return "mt5"
        if source == "remote":
            return "remote"
        if self.mt5_client.is_supported() and self.mt5_available():
            return "mt5"
        if self._remote_client.is_available():
            return "remote"
        return "local"

    def acquisition_provider(self) -> Union[MetaTraderClient, RemoteMt5Client]:
        """Provider used to *acquire* fresh history for ingestion.

        Preference: native MT5 (supported + available) → remote gateway (reachable).
        Raises ``ConnectionError`` when neither is reachable.
        """
        if self.mt5_client.is_supported() and self.mt5_available():
            return self.mt5_client
        if self._remote_client.is_available():
            return self._remote_client
        raise ConnectionError(
            "no acquisition provider: MetaTrader5 not installed and no reachable "
            "gateway"
        )

    def mt5_available(self) -> bool:
        return self.mt5_client.is_available()

    def acquisition_available(self) -> bool:
        """Whether fresh history can be acquired right now (native MT5 or gateway)."""
        try:
            self.acquisition_provider()
            return True
        except ConnectionError:
            return False

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
        except Exception as exc:  # noqa: BLE001 - startup connect probe; logged, returns False
            logger.warning("MetaTrader connect failed on startup: %s", exc)
            return False

    def shutdown(self) -> None:
        """
        Gracefully disconnects all market data clients.
        """
        logger.info("Shutting down market data clients...")
        self.mt5_client.disconnect()

    def _resolve_ohlcv_provider(self, symbol: str, timeframe: str):
        source = resolve_ohlcv_source(self, symbol, timeframe)
        if source == "local":
            return self._local_client
        if source == "remote":
            if not self._remote_client.is_supported():
                raise ConnectionError(_REMOTE_NOT_CONFIGURED)
            return self._remote_client
        if not self.mt5_client.is_supported():
            raise ConnectionError(
                "data_source is 'mt5' but MetaTrader5 is not installed on this "
                "platform. Install the Windows MetaTrader5 package or switch to "
                "'auto'/'local'."
            )
        return self.mt5_client

    def _fetch_through_ohlcv(
        self, symbol: str, timeframe: str, bars: List[OHLCV]
    ) -> None:
        """Write-behind persist of gateway-fetched bars into the local store.

        Best-effort: a parquet write failure must never fail or alter the read.
        """
        try:
            local_store.write_ohlcv(symbol, timeframe, bars)
        except Exception:  # noqa: BLE001 - fetch-through is best-effort; read must not fail
            logger.warning(
                "Fetch-through write_ohlcv failed for %s/%s; serving remote bars "
                "without caching.",
                symbol,
                timeframe,
                exc_info=True,
            )

    def _fetch_through_ticks(
        self, symbol: str, arrays: dict[str, np.ndarray]
    ) -> None:
        """Write-behind persist of gateway-fetched ticks into the local store."""
        try:
            local_store.write_ticks(symbol, arrays)
        except Exception:  # noqa: BLE001 - fetch-through is best-effort; read must not fail
            logger.warning(
                "Fetch-through write_ticks failed for %s; serving remote ticks "
                "without caching.",
                symbol,
                exc_info=True,
            )

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        return self._resolve_provider().get_symbol_info(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> List[OHLCV]:
        provider = self._resolve_ohlcv_provider(symbol, timeframe)
        bars = provider.get_ohlcv(symbol, timeframe, start, end)
        if provider is self._remote_client and bars:
            self._fetch_through_ohlcv(symbol, timeframe, bars)
        return bars

    def get_available_ohlcv_range(
        self, symbol: str, timeframe: str
    ) -> Optional[OhlcvAvailableRange]:
        return self._resolve_ohlcv_provider(symbol, timeframe).get_available_ohlcv_range(
            symbol, timeframe
        )

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
        provider = self._resolve_provider()
        arrays = provider.get_ticks_columnar(
            symbol, start, end, flags=flags, use_cache=use_cache
        )
        if provider is self._remote_client and len(arrays.get("time_msc", [])) > 0:
            self._fetch_through_ticks(symbol, arrays)
        return arrays

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> List[Tick]:
        return self._resolve_provider().get_recent_ticks(symbol, limit)

    def search_symbols(self, query: str) -> list:
        return self._resolve_provider().search_symbols(query)
