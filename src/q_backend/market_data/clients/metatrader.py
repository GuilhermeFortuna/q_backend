import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Union
import pandas as pd
import MetaTrader5 as mt5

from q_backend.market_data.models import OHLCV, Tick

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class OhlcvAvailableRange:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    bar_count: int


# Match the market OHLCV endpoint max; large single requests trigger MT5 "Invalid params".
_HISTORY_CHUNK_SIZE = 5_000
_MAX_HISTORY_CHUNKS = 1_000
_HISTORY_ANCHOR = datetime(1990, 1, 1)
_RANGE_PROBE_YEARS = 2


def _bar_open_time(rates, index: int) -> datetime:
    return datetime.fromtimestamp(int(rates[index]["time"]))

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M2": mt5.TIMEFRAME_M2,
    "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,
    "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10,
    "M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
    "H3": mt5.TIMEFRAME_H3,
    "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6,
    "H8": mt5.TIMEFRAME_H8,
    "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

class MetaTraderClient:
    """
    Client for interacting with the MetaTrader 5 local terminal.
    Requires the MetaTrader 5 terminal to be running on Windows.
    """
    def __init__(self, path: Optional[str] = None, login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None):
        self.path = path
        self.login = login
        self.password = password
        self.server = server
        self._is_initialized = False

    def connect(self) -> bool:
        """
        Establishes a connection to the MetaTrader 5 terminal.
        """
        if self._is_initialized:
            return True

        # 1. Initialize connection to MT5 terminal first
        init_kwargs = {}
        if self.path:
            init_kwargs["path"] = self.path
            
        if not mt5.initialize(**init_kwargs):
            error_code, error_desc = mt5.last_error()
            logger.error(f"Failed to initialize MetaTrader 5 terminal: {error_desc} (Code: {error_code})")
            return False

        # 2. Login to specific account if login details and server are provided
        if self.login is not None and self.server:
            password_param = self.password if self.password else ""
            logger.info(f"Logging into MT5 account {self.login} on server '{self.server}'...")
            if not mt5.login(login=self.login, password=password_param, server=self.server):
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to login into account {self.login}: {error_desc} (Code: {error_code})")
                mt5.shutdown()
                return False
        elif self.login is not None:
            logger.warning("MT5_USER was provided, but MT5_SERVER is missing. Skipping explicit login. Using currently active account in the terminal.")

        self._is_initialized = True
        logger.info("Successfully connected to MetaTrader 5 terminal.")
        return True

    def disconnect(self) -> None:
        """
        Closes the connection to the MetaTrader 5 terminal.
        """
        if self._is_initialized:
            mt5.shutdown()
            self._is_initialized = False
            logger.info("Disconnected from MetaTrader 5 terminal.")

    def _ensure_connected(self) -> None:
        if not self._is_initialized:
            if not self.connect():
                raise ConnectionError("Not connected to MetaTrader 5 terminal, and automatic reconnection failed.")

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves detailed information about a financial symbol.
        """
        self._ensure_connected()
        
        # Ensure the symbol is selected in MarketWatch
        if not mt5.symbol_select(symbol, True):
            error_code, error_desc = mt5.last_error()
            logger.error(f"Failed to select symbol {symbol}: {error_desc} (Code: {error_code})")
            return None

        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return info._asdict()

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime
    ) -> List[OHLCV]:
        """
        Fetches historical OHLCV data (bars) for a given symbol and timeframe within a range.
        """
        self._ensure_connected()

        mt5_timeframe = TIMEFRAME_MAP.get(timeframe.upper())
        if mt5_timeframe is None:
            raise ValueError(f"Invalid timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_MAP.keys())}")

        # Ensure the symbol is active in MarketWatch
        if not mt5.symbol_select(symbol, True):
            error_code, error_desc = mt5.last_error()
            logger.warning(f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})")

        # copy_rates_range takes datetime objects
        rates = mt5.copy_rates_range(symbol, mt5_timeframe, start, end)
        if rates is None or len(rates) == 0:
            error_code, error_desc = mt5.last_error()
            logger.error(f"Failed to fetch OHLCV for {symbol}: {error_desc} (Code: {error_code})")
            return []

        # Convert structured numpy array to pandas DataFrame
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        # Convert rows into OHLCV Pydantic objects
        ohlcv_list = []
        for _, row in df.iterrows():
            ohlcv_list.append(OHLCV(
                time=row['time'],
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                tick_volume=int(row['tick_volume']),
                spread=int(row['spread']) if 'spread' in row else None,
                real_volume=int(row['real_volume']) if 'real_volume' in row else None
            ))

        return ohlcv_list

    def _probe_latest_bar(self, symbol: str, mt5_timeframe: int) -> Optional[datetime]:
        rates = mt5.copy_rates_from_pos(symbol, mt5_timeframe, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        return _bar_open_time(rates, 0)

    def _probe_earliest_bar(self, symbol: str, mt5_timeframe: int) -> Optional[datetime]:
        """
        Find the oldest stored bar. copy_rates_from_pos only walks the terminal's
        in-memory window; copy_rates_from/range queries can reach further history.
        """
        rates = mt5.copy_rates_from(symbol, mt5_timeframe, _HISTORY_ANCHOR, 1)
        if rates is not None and len(rates) > 0:
            return _bar_open_time(rates, 0)

        earliest: Optional[datetime] = None
        cursor = _HISTORY_ANCHOR
        now = datetime.now()

        while cursor < now:
            chunk_end = min(
                datetime(cursor.year + _RANGE_PROBE_YEARS, cursor.month, cursor.day),
                now,
            )
            if chunk_end <= cursor:
                chunk_end = now

            rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
            if rates is not None and len(rates) > 0:
                chunk_earliest = _bar_open_time(rates, 0)
                earliest = (
                    chunk_earliest
                    if earliest is None
                    else min(earliest, chunk_earliest)
                )

            if chunk_end >= now:
                break
            cursor = chunk_end + timedelta(seconds=1)

        return earliest

    def _count_bars_between(
        self,
        symbol: str,
        mt5_timeframe: int,
        start: datetime,
        end: datetime,
    ) -> int:
        total = 0
        cursor = start

        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=365), end)
            rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
            if rates is not None and len(rates) > 0:
                total += len(rates)
                cursor = _bar_open_time(rates, -1) + timedelta(seconds=1)
            else:
                cursor = chunk_end + timedelta(seconds=1)

            if cursor > end:
                break

        return total

    def get_available_ohlcv_range(self, symbol: str, timeframe: str) -> Optional[OhlcvAvailableRange]:
        """
        Returns the earliest and latest bar timestamps available in MT5 for a symbol/timeframe.
        """
        self._ensure_connected()

        mt5_timeframe = TIMEFRAME_MAP.get(timeframe.upper())
        if mt5_timeframe is None:
            raise ValueError(f"Invalid timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_MAP.keys())}")

        if not mt5.symbol_select(symbol, True):
            error_code, error_desc = mt5.last_error()
            logger.warning(
                f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})"
            )
            return None

        earliest = self._probe_earliest_bar(symbol, mt5_timeframe)
        latest = self._probe_latest_bar(symbol, mt5_timeframe)

        if earliest is None or latest is None:
            error_code, error_desc = mt5.last_error()
            logger.error(
                f"Failed to probe OHLCV history for {symbol}: "
                f"{error_desc} (Code: {error_code})"
            )
            return None

        bar_count = self._count_bars_between(symbol, mt5_timeframe, earliest, latest)

        return OhlcvAvailableRange(
            symbol=symbol,
            timeframe=timeframe.upper(),
            start=earliest,
            end=latest,
            bar_count=bar_count,
        )

    def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int = mt5.COPY_TICKS_ALL
    ) -> List[Tick]:
        """
        Fetches historical ticks for a given symbol within a datetime range.
        """
        self._ensure_connected()

        # Ensure the symbol is active in MarketWatch
        if not mt5.symbol_select(symbol, True):
            error_code, error_desc = mt5.last_error()
            logger.warning(f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})")

        # copy_ticks_range takes datetime objects
        ticks = mt5.copy_ticks_range(symbol, start, end, flags)
        if ticks is None or len(ticks) == 0:
            error_code, error_desc = mt5.last_error()
            logger.error(f"Failed to fetch ticks for {symbol}: {error_desc} (Code: {error_code})")
            return []

        # Convert structured numpy array to pandas DataFrame
        df = pd.DataFrame(ticks)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        # Convert rows into Tick Pydantic objects
        tick_list = []
        for _, row in df.iterrows():
            tick_list.append(Tick(
                time=row['time'],
                bid=float(row['bid']),
                ask=float(row['ask']),
                last=float(row['last']) if 'last' in row else 0.0,
                volume=float(row['volume']) if 'volume' in row else 0.0,
                flags=int(row['flags']) if 'flags' in row else 0,
                time_msc=int(row['time_msc']) if 'time_msc' in row else 0
            ))

        return tick_list

    def search_symbols(self, query: str) -> List[Dict[str, Any]]:
        """
        Search for symbols in MetaTrader 5 using a wildcard pattern.
        """
        self._ensure_connected()
        pattern = f"*{query.upper()}*"
        symbols = mt5.symbols_get(pattern)
        if symbols is None:
            return []

        results = []
        for s in symbols:
            results.append({
                "name": s.name,
                "description": s.description,
                "path": s.path,
                "custom": s.custom
            })
        return results

