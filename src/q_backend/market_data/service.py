import os
import logging
from datetime import datetime
from typing import List, Optional
from pathlib import Path

from q_backend.market_data.clients.metatrader import MetaTraderClient
from q_backend.market_data.models import OHLCV, Tick

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
    High-level service managing market data connections and routing requests.
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

        logger.info(f"Initializing MarketDataService with MT5 User: {login}, Server: {server}")

        self.mt5_client = MetaTraderClient(
            path=path,
            login=login,
            password=password,
            server=server
        )

    def initialize(self) -> bool:
        """
        Initializes the underlying market data clients.
        """
        logger.info("Initializing MetaTrader client connection...")
        return self.mt5_client.connect()

    def shutdown(self) -> None:
        """
        Gracefully disconnects all market data clients.
        """
        logger.info("Shutting down market data clients...")
        self.mt5_client.disconnect()

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime
    ) -> List[OHLCV]:
        """
        Fetches OHLCV market data for a given symbol and timeframe.
        """
        return self.mt5_client.get_ohlcv(symbol, timeframe, start, end)

    def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime
    ) -> List[Tick]:
        """
        Fetches tick market data for a given symbol.
        """
        return self.mt5_client.get_ticks(symbol, start, end)
