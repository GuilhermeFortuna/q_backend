import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Union, Callable, TypeVar

import numpy as np
import MetaTrader5 as mt5

from q_backend.market_data.models import OHLCV, Tick
from q_backend.market_data.tick_cache import load as load_tick_cache
from q_backend.market_data.tick_cache import make_cache_key
from q_backend.market_data.tick_cache import store as store_tick_cache
from q_backend.market_data.timezone import (
    to_brasilia_naive,
    unix_seconds_to_brasilia_naive,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
_MAX_OHLCV_BARS = 50_000
_HISTORY_ANCHOR = datetime(1990, 1, 1)
_RANGE_PROBE_YEARS = 2
_RANGE_FETCH_DAYS = 365
_TICK_RANGE_FETCH_DAYS = 7
_MAX_TICKS = 50_000_000

COLUMNAR_TICK_KEYS = (
    "time_msc",
    "bid",
    "ask",
    "last",
    "volume",
    "flags",
)


def _to_naive_local(dt: datetime) -> datetime:
    """Convert aware datetimes to naive Brasília time to match MT5 bar timestamps."""
    return to_brasilia_naive(dt)


def _bar_open_time(rates, index: int) -> datetime:
    return unix_seconds_to_brasilia_naive(int(rates[index]["time"]))


def _empty_ticks_columnar() -> dict[str, np.ndarray]:
    return {
        "time_msc": np.array([], dtype=np.int64),
        "bid": np.array([], dtype=np.float64),
        "ask": np.array([], dtype=np.float64),
        "last": np.array([], dtype=np.float64),
        "volume": np.array([], dtype=np.float64),
        "flags": np.array([], dtype=np.int32),
    }


def _time_msc_to_naive_local(msc: int) -> datetime:
    sec = msc // 1000
    ms_remainder = msc % 1000
    base = unix_seconds_to_brasilia_naive(sec)
    return base + timedelta(milliseconds=ms_remainder)


def _ticks_structured_to_columnar(ticks: np.ndarray) -> dict[str, np.ndarray]:
    if ticks is None or len(ticks) == 0:
        return _empty_ticks_columnar()

    names = ticks.dtype.names or ()
    has_time_msc = "time_msc" in names
    has_last = "last" in names
    has_volume = "volume" in names
    has_flags = "flags" in names
    count = len(ticks)

    if has_time_msc:
        time_msc = ticks["time_msc"].astype(np.int64, copy=False)
    else:
        time_msc = ticks["time"].astype(np.int64, copy=False) * 1000

    bid = ticks["bid"].astype(np.float64, copy=False)
    ask = ticks["ask"].astype(np.float64, copy=False)
    last = (
        ticks["last"].astype(np.float64, copy=False)
        if has_last
        else np.zeros(count, dtype=np.float64)
    )
    volume = (
        ticks["volume"].astype(np.float64, copy=False)
        if has_volume
        else np.zeros(count, dtype=np.float64)
    )
    flags = (
        ticks["flags"].astype(np.int32, copy=False)
        if has_flags
        else np.zeros(count, dtype=np.int32)
    )

    return {
        "time_msc": time_msc,
        "bid": bid,
        "ask": ask,
        "last": last,
        "volume": volume,
        "flags": flags,
    }


def _map_tick_rows(ticks) -> List[Tick]:
    """Map a raw MT5 tick array to a list of Tick models (preserving order)."""
    has_last = "last" in ticks.dtype.names
    has_volume = "volume" in ticks.dtype.names
    has_flags = "flags" in ticks.dtype.names
    has_time_msc = "time_msc" in ticks.dtype.names

    return [
        Tick(
            time=unix_seconds_to_brasilia_naive(int(ticks["time"][i])),
            bid=float(ticks["bid"][i]),
            ask=float(ticks["ask"][i]),
            last=float(ticks["last"][i]) if has_last else 0.0,
            volume=float(ticks["volume"][i]) if has_volume else 0.0,
            flags=int(ticks["flags"][i]) if has_flags else 0,
            time_msc=int(ticks["time_msc"][i]) if has_time_msc else 0,
        )
        for i in range(len(ticks))
    ]


# Escalating look-back windows for fetching the most recent ticks. We widen the
# range until we have enough ticks (covers off-hours / illiquid symbols) without
# scanning unbounded history.
_RECENT_TICKS_WINDOWS = (
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
)


def _rates_to_ohlcv_list(rates) -> List[OHLCV]:
    """Convert MT5 structured numpy array to OHLCV models without row iteration."""
    if rates is None or len(rates) == 0:
        return []

    has_spread = "spread" in rates.dtype.names
    has_real_volume = "real_volume" in rates.dtype.names

    return [
        OHLCV(
            time=unix_seconds_to_brasilia_naive(int(rates["time"][i])),
            open=float(rates["open"][i]),
            high=float(rates["high"][i]),
            low=float(rates["low"][i]),
            close=float(rates["close"][i]),
            tick_volume=int(rates["tick_volume"][i]),
            spread=int(rates["spread"][i]) if has_spread else None,
            real_volume=int(rates["real_volume"][i]) if has_real_volume else None,
        )
        for i in range(len(rates))
    ]


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

    def __init__(
        self,
        path: Optional[str] = None,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
    ):
        self.path = path
        self.login = login
        self.password = password
        self.server = server
        self._is_initialized = False
        self._lock = threading.Lock()

    def _run_locked(self, fn: Callable[[], T]) -> T:
        with self._lock:
            return fn()

    def connect(self) -> bool:
        """
        Establishes a connection to the MetaTrader 5 terminal.
        """

        def _connect() -> bool:
            if self._is_initialized:
                return True

            init_kwargs = {}
            if self.path:
                init_kwargs["path"] = self.path

            if not mt5.initialize(**init_kwargs):
                error_code, error_desc = mt5.last_error()
                logger.error(
                    f"Failed to initialize MetaTrader 5 terminal: {error_desc} (Code: {error_code})"
                )
                return False

            if self.login is not None and self.server:
                password_param = self.password if self.password else ""
                logger.info(
                    f"Logging into MT5 account {self.login} on server '{self.server}'..."
                )
                if not mt5.login(
                    login=self.login, password=password_param, server=self.server
                ):
                    error_code, error_desc = mt5.last_error()
                    logger.error(
                        f"Failed to login into account {self.login}: {error_desc} (Code: {error_code})"
                    )
                    mt5.shutdown()
                    return False
            elif self.login is not None:
                logger.warning(
                    "MT5_USER was provided, but MT5_SERVER is missing. Skipping explicit login. Using currently active account in the terminal."
                )

            self._is_initialized = True
            logger.info("Successfully connected to MetaTrader 5 terminal.")
            return True

        return self._run_locked(_connect)

    def disconnect(self) -> None:
        """
        Closes the connection to the MetaTrader 5 terminal.
        """

        def _disconnect() -> None:
            if self._is_initialized:
                mt5.shutdown()
                self._is_initialized = False
                logger.info("Disconnected from MetaTrader 5 terminal.")

        self._run_locked(_disconnect)

    def _ensure_connected(self) -> None:
        if not self._is_initialized:
            if not self.connect():
                raise ConnectionError(
                    "Not connected to MetaTrader 5 terminal, and automatic reconnection failed."
                )

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves detailed information about a financial symbol.
        """

        def _fetch() -> Optional[Dict[str, Any]]:
            self._ensure_connected()

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.error(
                    f"Failed to select symbol {symbol}: {error_desc} (Code: {error_code})"
                )
                return None

            info = mt5.symbol_info(symbol)
            if info is None:
                return None
            return info._asdict()

        return self._run_locked(_fetch)

    def _fetch_ohlcv_range_chunked(
        self,
        symbol: str,
        mt5_timeframe: int,
        start: datetime,
        end: datetime,
    ) -> List[OHLCV]:
        start = _to_naive_local(start)
        end = _to_naive_local(end)

        chunks: List[np.ndarray] = []
        total_bars = 0
        cursor = start

        for _ in range(_MAX_HISTORY_CHUNKS):
            if cursor > end:
                break

            chunk_end = min(cursor + timedelta(days=_RANGE_FETCH_DAYS), end)
            rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)

            if rates is not None and len(rates) > 0:
                chunk_len = len(rates)
                if total_bars + chunk_len > _MAX_OHLCV_BARS:
                    remaining = _MAX_OHLCV_BARS - total_bars
                    if remaining <= 0:
                        break
                    rates = rates[:remaining]
                    chunk_len = remaining

                chunks.append(rates)
                total_bars += chunk_len
                next_cursor = _bar_open_time(rates, -1) + timedelta(seconds=1)
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
            else:
                cursor = chunk_end + timedelta(seconds=1)

            if total_bars >= _MAX_OHLCV_BARS:
                break

        if not chunks:
            return []

        all_rates = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return _rates_to_ohlcv_list(all_rates)

    def get_ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> List[OHLCV]:
        """
        Fetches historical OHLCV data (bars) for a given symbol and timeframe within a range.
        """

        def _fetch() -> List[OHLCV]:
            self._ensure_connected()

            mt5_timeframe = TIMEFRAME_MAP.get(timeframe.upper())
            if mt5_timeframe is None:
                raise ValueError(
                    f"Invalid timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_MAP.keys())}"
                )

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})"
                )

            result = self._fetch_ohlcv_range_chunked(symbol, mt5_timeframe, start, end)
            if not result:
                error_code, error_desc = mt5.last_error()
                logger.error(
                    f"Failed to fetch OHLCV for {symbol}: {error_desc} (Code: {error_code})"
                )
            return result

        return self._run_locked(_fetch)

    def _probe_latest_bar(self, symbol: str, mt5_timeframe: int) -> Optional[datetime]:
        rates = mt5.copy_rates_from_pos(symbol, mt5_timeframe, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        return _bar_open_time(rates, 0)

    def _probe_earliest_bar(
        self, symbol: str, mt5_timeframe: int
    ) -> Optional[datetime]:
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
        start = _to_naive_local(start)
        end = _to_naive_local(end)

        total = 0
        cursor = start

        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=_RANGE_FETCH_DAYS), end)
            rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
            if rates is not None and len(rates) > 0:
                total += len(rates)
                cursor = _bar_open_time(rates, -1) + timedelta(seconds=1)
            else:
                cursor = chunk_end + timedelta(seconds=1)

            if cursor > end:
                break

        return total

    def get_available_ohlcv_range(
        self, symbol: str, timeframe: str
    ) -> Optional[OhlcvAvailableRange]:
        """
        Returns the earliest and latest bar timestamps available in MT5 for a symbol/timeframe.
        """

        def _fetch() -> Optional[OhlcvAvailableRange]:
            self._ensure_connected()

            mt5_timeframe = TIMEFRAME_MAP.get(timeframe.upper())
            if mt5_timeframe is None:
                raise ValueError(
                    f"Invalid timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_MAP.keys())}"
                )

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

            bar_count = self._count_bars_between(
                symbol, mt5_timeframe, earliest, latest
            )

            return OhlcvAvailableRange(
                symbol=symbol,
                timeframe=timeframe.upper(),
                start=earliest,
                end=latest,
                bar_count=bar_count,
            )

        return self._run_locked(_fetch)

    def _fetch_ticks_range_chunked(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int,
    ) -> dict[str, np.ndarray]:
        start = _to_naive_local(start)
        end = _to_naive_local(end)

        chunks: List[np.ndarray] = []
        total_ticks = 0
        cursor = start

        for _ in range(_MAX_HISTORY_CHUNKS):
            if cursor > end:
                break

            chunk_end = min(
                cursor + timedelta(days=_TICK_RANGE_FETCH_DAYS), end
            )
            ticks = mt5.copy_ticks_range(symbol, cursor, chunk_end, flags)

            if ticks is not None and len(ticks) > 0:
                chunk_len = len(ticks)
                if total_ticks + chunk_len > _MAX_TICKS:
                    raise ValueError(
                        f"Tick range for {symbol} exceeds the maximum of "
                        f"{_MAX_TICKS:,} ticks; narrow the start/end window."
                    )

                chunks.append(ticks)
                total_ticks += chunk_len

                names = ticks.dtype.names or ()
                if "time_msc" in names:
                    last_msc = int(ticks["time_msc"][-1])
                    next_cursor = _time_msc_to_naive_local(last_msc) + timedelta(
                        milliseconds=1
                    )
                else:
                    last_sec = int(ticks["time"][-1])
                    next_cursor = unix_seconds_to_brasilia_naive(last_sec) + timedelta(
                        seconds=1
                    )

                if next_cursor <= cursor:
                    break
                cursor = next_cursor
            else:
                cursor = chunk_end + timedelta(seconds=1)

            if total_ticks >= _MAX_TICKS:
                break

        if not chunks:
            return _empty_ticks_columnar()

        all_ticks = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return _ticks_structured_to_columnar(all_ticks)

    def get_ticks_columnar(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int = mt5.COPY_TICKS_ALL,
        use_cache: bool = True,
    ) -> dict[str, np.ndarray]:
        """
        Returns aligned NumPy arrays for the range (no per-row Python objects):
          {
            "time_msc": int64[],   # epoch milliseconds, the canonical ordering key
            "bid":      float64[],
            "ask":      float64[],
            "last":     float64[],
            "volume":   float64[],
            "flags":    int32[],
          }
        All arrays share length N and are sorted ascending by time_msc.
        Returns empty arrays (length 0) when the range has no ticks.
        """

        def _fetch() -> dict[str, np.ndarray]:
            self._ensure_connected()

            start_local = _to_naive_local(start)
            end_local = _to_naive_local(end)
            cache_key: Optional[str] = None

            if use_cache:
                cache_key = make_cache_key(symbol, start_local, end_local, flags)
                cached = load_tick_cache(cache_key)
                if cached is not None:
                    return cached

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"Failed to select symbol {symbol} in MarketWatch: "
                    f"{error_desc} (Code: {error_code})"
                )

            result = self._fetch_ticks_range_chunked(
                symbol, start_local, end_local, flags
            )

            if use_cache and cache_key is not None:
                store_tick_cache(cache_key, result)

            return result

        return self._run_locked(_fetch)

    def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int = mt5.COPY_TICKS_ALL,
    ) -> List[Tick]:
        """
        Fetches historical ticks for a given symbol within a datetime range.
        """

        def _fetch() -> List[Tick]:
            self._ensure_connected()

            start = _to_naive_local(start)
            end = _to_naive_local(end)

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})"
                )

            ticks = mt5.copy_ticks_range(symbol, start, end, flags)
            if ticks is None or len(ticks) == 0:
                error_code, error_desc = mt5.last_error()
                logger.error(
                    f"Failed to fetch ticks for {symbol}: {error_desc} (Code: {error_code})"
                )
                return []

            return _map_tick_rows(ticks)

        return self._run_locked(_fetch)

    def get_recent_ticks(self, symbol: str, limit: int = 200) -> List[Tick]:
        """
        Fetches the most recent ticks for a symbol (newest last), capped at `limit`.

        Uses ``copy_ticks_range`` over an escalating look-back window rather than
        ``copy_ticks_from`` with a negative count (which does not return recent
        history). The window widens until at least ``limit`` ticks are found or the
        largest window is exhausted, then the newest ``limit`` ticks are returned.
        """

        def _fetch() -> List[Tick]:
            self._ensure_connected()

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"Failed to select symbol {symbol} in MarketWatch: "
                    f"{error_desc} (Code: {error_code})"
                )
                return []

            end = datetime.now()
            mapped: List[Tick] = []
            for window in _RECENT_TICKS_WINDOWS:
                start = _to_naive_local(end - window)
                ticks = mt5.copy_ticks_range(
                    symbol, start, _to_naive_local(end), mt5.COPY_TICKS_ALL
                )
                if ticks is None or len(ticks) == 0:
                    continue

                mapped = _map_tick_rows(ticks)
                if len(mapped) >= limit:
                    break

            if not mapped:
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"No recent ticks for {symbol} within "
                    f"{_RECENT_TICKS_WINDOWS[-1]}: {error_desc} (Code: {error_code})"
                )
                return []

            return mapped[-limit:]

        return self._run_locked(_fetch)

    def search_symbols(self, query: str) -> List[Dict[str, Any]]:
        """
        Search for symbols in MetaTrader 5 using a wildcard pattern.
        """

        def _fetch() -> List[Dict[str, Any]]:
            self._ensure_connected()
            pattern = f"*{query.upper()}*"
            symbols = mt5.symbols_get(pattern)
            if symbols is None:
                return []

            return [
                {
                    "name": s.name,
                    "description": s.description,
                    "path": s.path,
                    "custom": s.custom,
                }
                for s in symbols
            ]

        return self._run_locked(_fetch)
