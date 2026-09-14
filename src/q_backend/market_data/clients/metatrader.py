import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Union, Callable, TypeVar

import numpy as np

from q_backend.market_data.clients.shared import (
    COPY_TICKS_ALL,
    COPY_TICKS_TRADE,
    OhlcvAvailableRange,
    _RECENT_TICKS_WINDOWS,
    _empty_ticks_columnar,
    _time_msc_to_naive_local,
)

try:
    import MetaTrader5 as mt5

    MT5_IMPORTABLE = True
except Exception:  # noqa: BLE001 - optional dep guard: ImportError on Linux, DLL errors on Windows
    # Best-effort optional-dependency guard. MetaTrader5 is Windows-only and can
    # fail to import for many reasons (missing module, DLL load errors); we fall
    # back to the stub and record importability rather than crash at module load.
    mt5 = None  # type: ignore[assignment]
    MT5_IMPORTABLE = False

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


def _naive_local_to_time_msc(dt: datetime) -> int:
    """Inverse of ``_time_msc_to_naive_local`` for range filtering."""
    dt = _to_naive_local(dt)
    ms = dt.microsecond // 1000
    base = dt.replace(microsecond=0)
    utc_wall = base.replace(tzinfo=timezone.utc)
    return int(utc_wall.timestamp()) * 1000 + ms


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
    last = ticks["last"].astype(np.float64, copy=False) if has_last else np.zeros(count, dtype=np.float64)
    volume = ticks["volume"].astype(np.float64, copy=False) if has_volume else np.zeros(count, dtype=np.float64)
    flags = ticks["flags"].astype(np.int32, copy=False) if has_flags else np.zeros(count, dtype=np.int32)

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


TIMEFRAME_NAMES = (
    "M1",
    "M2",
    "M3",
    "M4",
    "M5",
    "M6",
    "M10",
    "M12",
    "M15",
    "M20",
    "M30",
    "H1",
    "H2",
    "H3",
    "H4",
    "H6",
    "H8",
    "H12",
    "D1",
    "W1",
    "MN1",
)

TIMEFRAME_MAP = {name: name for name in TIMEFRAME_NAMES}


def _mt5_timeframe(name: str) -> int:
    if mt5 is None:
        raise RuntimeError(
            "MetaTrader5 is not available on this platform. "
            "Install the Windows MetaTrader5 package or use data_source='local'."
        )
    attr = f"TIMEFRAME_{name}"
    value = getattr(mt5, attr, None)
    if value is None:
        raise ValueError(f"Unknown MT5 timeframe constant: {attr}")
    return int(value)


def _resolve_mt5_timeframe(timeframe: str) -> int:
    name = timeframe.upper()
    if name not in TIMEFRAME_MAP:
        raise ValueError(f"Invalid timeframe '{timeframe}'. Choose from: {list(TIMEFRAME_MAP.keys())}")
    return _mt5_timeframe(name)


TICK_FLAG_BUY = int(getattr(mt5, "TICK_FLAG_BUY", 32)) if mt5 is not None else 32
TICK_FLAG_SELL = int(getattr(mt5, "TICK_FLAG_SELL", 64)) if mt5 is not None else 64


def resolve_copy_ticks_flags(tick_flags: str | None) -> int:
    if tick_flags is None or tick_flags.lower() == "all":
        return COPY_TICKS_ALL
    if tick_flags.lower() == "trade":
        return COPY_TICKS_TRADE
    raise ValueError(f"Invalid tick_flags '{tick_flags}'. Expected 'all' or 'trade'.")


def _get_chunk_days(mt5_timeframe: int) -> int:
    """
    Returns the optimal number of days for chunked OHLCV fetching.
    Prevents MT5 'Invalid params' error (-2) by keeping the requested
    number of bars within the terminal's/broker's single-request limits.
    """
    if mt5_timeframe == mt5.TIMEFRAME_M1:
        return 15
    elif mt5_timeframe in (
        mt5.TIMEFRAME_M2,
        mt5.TIMEFRAME_M3,
        mt5.TIMEFRAME_M4,
        mt5.TIMEFRAME_M5,
        mt5.TIMEFRAME_M6,
    ):
        return 60
    elif mt5_timeframe in (
        mt5.TIMEFRAME_M10,
        mt5.TIMEFRAME_M12,
        mt5.TIMEFRAME_M15,
        mt5.TIMEFRAME_M20,
        mt5.TIMEFRAME_M30,
    ):
        return 180
    else:
        # H1, H4, D1, etc.
        return 365


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
        # Reentrant: locked fetch paths call _ensure_connected -> connect, which
        # takes the lock again; a plain Lock self-deadlocks and starves the API
        # threadpool.
        self._lock = threading.RLock()

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

            if mt5 is None:
                logger.error("MetaTrader5 package is not installed or failed to import.")
                return False

            init_kwargs = {}
            if self.path:
                init_kwargs["path"] = self.path

            if not mt5.initialize(**init_kwargs):
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to initialize MetaTrader 5 terminal: {error_desc} (Code: {error_code})")
                return False

            if self.login is not None and self.server:
                password_param = self.password if self.password else ""
                logger.info(f"Logging into MT5 account {self.login} on server '{self.server}'...")
                if not mt5.login(login=self.login, password=password_param, server=self.server):
                    error_code, error_desc = mt5.last_error()
                    logger.error(f"Failed to login into account {self.login}: {error_desc} (Code: {error_code})")
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

    def is_supported(self) -> bool:
        """Whether this platform can run a real MT5 terminal (not the Linux stub).

        Use this to decide *which* provider to route to; use ``is_available`` only
        to report live terminal connectivity.
        """
        if mt5 is None or not MT5_IMPORTABLE:
            return False
        if getattr(mt5, "IS_STUB", False):
            return False
        return True

    def is_available(self) -> bool:
        if not self.is_supported():
            return False
        try:
            return self.connect()
        except Exception:  # noqa: BLE001 - availability probe reports False on any failure
            # Best-effort probe: any connection failure means "not available".
            logger.warning("MT5 availability check failed", exc_info=True)
            return False

    def _ensure_connected(self) -> None:
        if not self._is_initialized:
            if not self.connect():
                raise ConnectionError("Not connected to MetaTrader 5 terminal, and automatic reconnection failed.")

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves detailed information about a financial symbol.
        """

        def _fetch() -> Optional[Dict[str, Any]]:
            self._ensure_connected()

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to select symbol {symbol}: {error_desc} (Code: {error_code})")
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
        chunk_days = _get_chunk_days(mt5_timeframe)

        for _ in range(_MAX_HISTORY_CHUNKS):
            if cursor > end:
                break

            chunk_end = min(cursor + timedelta(days=chunk_days), end)
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

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[OHLCV]:
        """
        Fetches historical OHLCV data (bars) for a given symbol and timeframe within a range.
        """

        def _fetch() -> List[OHLCV]:
            self._ensure_connected()

            mt5_timeframe = _resolve_mt5_timeframe(timeframe)

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})")

            result = self._fetch_ohlcv_range_chunked(symbol, mt5_timeframe, start, end)
            if not result:
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to fetch OHLCV for {symbol}: {error_desc} (Code: {error_code})")
            return result

        return self._run_locked(_fetch)

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
        chunk_days = _get_chunk_days(mt5_timeframe)

        while cursor < now:
            chunk_end = min(
                cursor + timedelta(days=chunk_days),
                now,
            )
            if chunk_end <= cursor:
                chunk_end = now

            rates = mt5.copy_rates_range(symbol, mt5_timeframe, cursor, chunk_end)
            if rates is not None and len(rates) > 0:
                chunk_earliest = _bar_open_time(rates, 0)
                earliest = chunk_earliest if earliest is None else min(earliest, chunk_earliest)

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
        chunk_days = _get_chunk_days(mt5_timeframe)

        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
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

        def _fetch() -> Optional[OhlcvAvailableRange]:
            self._ensure_connected()

            mt5_timeframe = _resolve_mt5_timeframe(timeframe)

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})")
                return None

            earliest = self._probe_earliest_bar(symbol, mt5_timeframe)
            latest = self._probe_latest_bar(symbol, mt5_timeframe)

            if earliest is None or latest is None:
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to probe OHLCV history for {symbol}: " f"{error_desc} (Code: {error_code})")
                return None

            bar_count = self._count_bars_between(symbol, mt5_timeframe, earliest, latest)

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

            chunk_end = min(cursor + timedelta(days=_TICK_RANGE_FETCH_DAYS), end)
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
                    next_cursor = _time_msc_to_naive_local(last_msc) + timedelta(milliseconds=1)
                else:
                    last_sec = int(ticks["time"][-1])
                    next_cursor = unix_seconds_to_brasilia_naive(last_sec) + timedelta(seconds=1)

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
        flags: int | None = None,
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

            resolved_flags = _default_copy_ticks_all() if flags is None else flags
            start_local = _to_naive_local(start)
            end_local = _to_naive_local(end)
            cache_key: Optional[str] = None

            if use_cache:
                cache_key = make_cache_key(symbol, start_local, end_local, resolved_flags)
                cached = load_tick_cache(cache_key)
                if cached is not None:
                    return cached

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(
                    f"Failed to select symbol {symbol} in MarketWatch: " f"{error_desc} (Code: {error_code})"
                )

            result = self._fetch_ticks_range_chunked(symbol, start_local, end_local, resolved_flags)

            if use_cache and cache_key is not None:
                store_tick_cache(cache_key, result)

            return result

        return self._run_locked(_fetch)

    def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int | None = None,
    ) -> List[Tick]:
        """
        Fetches historical ticks for a given symbol within a datetime range.
        """

        def _fetch() -> List[Tick]:
            self._ensure_connected()

            resolved_flags = _default_copy_ticks_all() if flags is None else flags
            start = _to_naive_local(start)
            end = _to_naive_local(end)

            if not mt5.symbol_select(symbol, True):
                error_code, error_desc = mt5.last_error()
                logger.warning(f"Failed to select symbol {symbol} in MarketWatch: {error_desc} (Code: {error_code})")

            ticks = mt5.copy_ticks_range(symbol, start, end, resolved_flags)
            if ticks is None or len(ticks) == 0:
                error_code, error_desc = mt5.last_error()
                logger.error(f"Failed to fetch ticks for {symbol}: {error_desc} (Code: {error_code})")
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
                    f"Failed to select symbol {symbol} in MarketWatch: " f"{error_desc} (Code: {error_code})"
                )
                return []

            end = datetime.now()
            mapped: List[Tick] = []
            for window in _RECENT_TICKS_WINDOWS:
                start = _to_naive_local(end - window)
                ticks = mt5.copy_ticks_range(symbol, start, _to_naive_local(end), mt5.COPY_TICKS_ALL)
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
