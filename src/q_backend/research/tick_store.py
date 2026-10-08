"""File-only per-session tick store for offline research."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.data import _resample_frequency, _ticks_frame_from_columnar
from q_backend.research.errors import NoMarketDataError
from q_backend.research.frame import (
    FRAME_COLUMNS,
    PUBLIC_TZ_NAME,
    attach_metadata,
    bounds_to_brasilia_naive,
    filter_index_range,
    normalize_timeframe,
    parse_query_bound,
    validate_bars_frame,
)
from q_backend.research.providers import resolve_gateway_token, resolve_gateway_url

TICK_STORE_SOURCE = "tick_store"

_SYNC_HINT = (
    "Sync from the MT5 gateway: run `uv run q-sync-ticks --symbol <SYM> --start YYYY-MM-DD` "
    "(with the gateway up), or pass `sync=True` to TickStore.bars(...)."
)

_COLUMNAR_KEYS = ("time_msc", "bid", "ask", "last", "volume", "flags")
_DTYPE_MAP = {
    "time_msc": np.int64,
    "bid": np.float64,
    "ask": np.float64,
    "last": np.float64,
    "volume": np.float64,
    "flags": np.int32,
}
_TRADE_FLAG_MASK = 32 | 64


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _slug_symbol(symbol: str) -> str:
    return re.sub(r"[^\w.$-]+", "_", symbol)


def _resolve_root(explicit: str | Path | None) -> Path:
    if explicit is not None:
        path = Path(explicit)
    else:
        env = os.getenv("Q_RESEARCH_TICK_STORE")
        if env:
            path = Path(env)
        else:
            path = Path("data/tick_store")
    if not path.is_absolute():
        path = _project_root() / path
    return path


def _exchange_now() -> datetime:
    return datetime.now(BRASILIA_TZ)


def _resolve_sync_end(end: str | datetime | None, *, now: datetime) -> date:
    if end is None:
        yesterday = (now - timedelta(days=1)).date()
        return yesterday
    ts = parse_query_bound(end)
    return ts.date()


def _resolve_sync_start(start: str | datetime) -> date:
    return parse_query_bound(start).date()


def _iter_weekdays(start_day: date, end_day: date) -> list[date]:
    if start_day > end_day:
        raise ValueError("start must be <= end")
    days: list[date] = []
    current = start_day
    while current <= end_day:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 0, 0)
    end = start + timedelta(days=1)
    return start, end


def _sort_columnar(arrays: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    if arrays["time_msc"].size == 0:
        return {key: np.asarray(arrays[key], dtype=_DTYPE_MAP[key]) for key in _COLUMNAR_KEYS}
    order = np.argsort(arrays["time_msc"], kind="stable")
    return {key: np.asarray(arrays[key], dtype=_DTYPE_MAP[key])[order] for key in _COLUMNAR_KEYS}


def _write_day_atomic(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_arrays = _sort_columnar(arrays)
    table = pa.table({col: sorted_arrays[col] for col in _COLUMNAR_KEYS})
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".parquet.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        pq.write_table(table, tmp_path, compression="zstd")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _read_day_columnar(path: Path) -> dict[str, np.ndarray]:
    table = pq.read_table(path)
    return {
        col: table[col].to_numpy(zero_copy_only=False).astype(_DTYPE_MAP[col], copy=False) for col in _COLUMNAR_KEYS
    }


def _m1_from_columnar(arrays: dict[str, np.ndarray]) -> pd.DataFrame:
    from q_backend.market_data.clients.shared import _time_msc_to_naive_local

    if arrays["time_msc"].size == 0:
        return pd.DataFrame(columns=list(FRAME_COLUMNS), index=pd.DatetimeIndex([], tz=BRASILIA_TZ, name="time"))
    times = pd.DatetimeIndex(
        [_time_msc_to_naive_local(int(value)) for value in arrays["time_msc"]],
        name="time",
    ).tz_localize(BRASILIA_TZ)
    flags = np.asarray(arrays["flags"], dtype=np.int64)
    last = np.asarray(arrays["last"], dtype=np.float64)
    volume = np.asarray(arrays["volume"], dtype=np.float64)
    ticks = pd.DataFrame({"last": last, "volume": volume, "flags": flags}, index=times).sort_index(kind="stable")
    m1 = _resample_ticks_with_real_volume(ticks)
    return m1


def _resample_ticks_with_real_volume(ticks: pd.DataFrame) -> pd.DataFrame:
    """M1 bars with ``real_volume``; omits intervals without a trade price."""
    if not isinstance(ticks.index, pd.DatetimeIndex):
        raise ValueError("ticks must have a DatetimeIndex")
    if ticks.index.tz is None:
        raise ValueError("ticks index must be timezone-aware")
    prices = pd.to_numeric(ticks["last"], errors="raise").astype("float64")
    valid = prices.notna() & np.isfinite(prices) & (prices > 0)
    if valid.sum() == 0:
        return pd.DataFrame(columns=list(FRAME_COLUMNS), index=pd.DatetimeIndex([], tz=ticks.index.tz, name="time"))

    trade_mask = (ticks["flags"].to_numpy(dtype=np.int64) & _TRADE_FLAG_MASK) != 0
    trade_vol = np.where(trade_mask, ticks["volume"].to_numpy(dtype=np.float64), 0.0)
    work = ticks.loc[valid].copy()
    work["_trade_vol"] = trade_vol[valid.to_numpy()]

    grouped = work["last"].resample("1min", closed="left", label="left", origin="start_day")
    bars = grouped.ohlc()
    bars["tick_volume"] = grouped.count().astype("int64")
    trade_grouped = work["_trade_vol"].resample("1min", closed="left", label="left", origin="start_day")
    bars["real_volume"] = trade_grouped.sum().astype("float64")
    bars["spread"] = np.nan
    bars = bars[bars["tick_volume"] > 0]
    for col in FRAME_COLUMNS:
        if col not in bars.columns:
            bars[col] = np.nan
    bars = bars[list(FRAME_COLUMNS)]
    bars["tick_volume"] = bars["tick_volume"].astype("int64")
    bars["spread"] = bars["spread"].astype("float64")
    bars["real_volume"] = bars["real_volume"].astype("float64")
    return bars


def _aggregate_bars(m1: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    tf = normalize_timeframe(timeframe)
    if tf == "M1":
        return m1
    if tf in ("W1", "MN1"):
        raise ValueError(f"Unsupported timeframe '{timeframe}' for tick store bars")
    frequency = _resample_frequency(tf)
    if m1.empty:
        return m1.copy()
    grouped = m1.resample(frequency, closed="left", label="left", origin="start_day")
    bars = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "tick_volume": grouped["tick_volume"].sum(),
            "real_volume": grouped["real_volume"].sum(),
            "spread": np.nan,
        },
        index=grouped.size().index,
    )
    bars = bars[bars["tick_volume"] > 0]
    bars["tick_volume"] = bars["tick_volume"].astype("int64")
    bars["spread"] = bars["spread"].astype("float64")
    bars["real_volume"] = bars["real_volume"].astype("float64")
    return bars[list(FRAME_COLUMNS)]


@dataclass
class TickSyncReport:
    stored: list[date]
    already_present: list[date]
    empty: list[date]
    unsettled: list[date]
    failed: list[date]

    def __str__(self) -> str:
        lines: list[str] = []

        def _line(label: str, days: list[date]) -> None:
            if not days:
                return
            text = ", ".join(day.isoformat() for day in days)
            lines.append(f"{label}: {text}")

        _line("stored", self.stored)
        _line("already present", self.already_present)
        _line("empty", self.empty)
        _line("unsettled", self.unsettled)
        _line("failed", self.failed)
        return "\n".join(lines) if lines else "no days processed"


class TickStore:
    def __init__(self, symbol: str, *, root: str | Path | None = None) -> None:
        sym = symbol.strip()
        if not sym:
            raise ValueError("symbol must be non-empty")
        self._symbol = sym
        self._root = _resolve_root(root)
        self._slug = _slug_symbol(sym)
        self._trade_prices_cache_day: date | None = None
        self._trade_prices_cache: dict[str, np.ndarray] | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    def _symbol_dir(self) -> Path:
        return self._root / self._slug

    def _day_path(self, day: date) -> Path:
        return self._symbol_dir() / f"{day.isoformat()}.parquet"

    def _m1_cache_path(self, day: date) -> Path:
        return self._symbol_dir() / "bars_M1" / f"{day.isoformat()}.parquet"

    def sessions(self) -> list[date]:
        directory = self._symbol_dir()
        if not directory.is_dir():
            return []
        days: list[date] = []
        for path in sorted(directory.glob("*.parquet")):
            try:
                days.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
        return days

    def sync(
        self,
        *,
        start: str | datetime,
        end: str | datetime | None = None,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
    ) -> TickSyncReport:
        captured_now = _exchange_now()
        start_day = _resolve_sync_start(start)
        end_day = _resolve_sync_end(end, now=captured_now)
        today = captured_now.date()
        resolved_url = resolve_gateway_url(gateway_url)
        if not resolved_url:
            raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")
        resolved_token = resolve_gateway_token(gateway_token)
        client = RemoteMt5Client(base_url=resolved_url, token=resolved_token)
        if not client.is_supported():
            raise ValueError("MT5 gateway URL is not configured. Set Q_MT5_GATEWAY_URL or pass gateway_url=.")

        stored: list[date] = []
        already_present: list[date] = []
        empty: list[date] = []
        unsettled: list[date] = []
        failed: list[date] = []

        for day in _iter_weekdays(start_day, end_day):
            if day >= today:
                continue
            path = self._day_path(day)
            if path.is_file():
                already_present.append(day)
                continue
            day_start, day_end = _day_bounds(day)
            try:
                first = client.get_ticks_columnar(
                    self._symbol,
                    day_start,
                    day_end,
                    flags=COPY_TICKS_ALL,
                    use_cache=False,
                )
                second = client.get_ticks_columnar(
                    self._symbol,
                    day_start,
                    day_end,
                    flags=COPY_TICKS_ALL,
                    use_cache=False,
                )
            except Exception:  # noqa: BLE001 - per-day gateway failure must not stop sync
                failed.append(day)
                continue
            count_first = int(first["time_msc"].size)
            count_second = int(second["time_msc"].size)
            if count_first == 0 and count_second == 0:
                empty.append(day)
                continue
            if count_first != count_second:
                unsettled.append(day)
                continue
            if count_second == 0:
                empty.append(day)
                continue
            _write_day_atomic(path, second)
            stored.append(day)

        return TickSyncReport(
            stored=stored,
            already_present=already_present,
            empty=empty,
            unsettled=unsettled,
            failed=failed,
        )

    def _resolve_read_bounds(
        self,
        start: str | datetime,
        end: str | datetime | None,
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        captured_now = _exchange_now()
        start_ts = parse_query_bound(start)
        if end is None:
            end_ts = pd.Timestamp(captured_now)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize(BRASILIA_TZ)
            else:
                end_ts = end_ts.tz_convert(BRASILIA_TZ)
        else:
            end_ts = parse_query_bound(end)
        if start_ts > end_ts:
            raise ValueError("start must be <= end")
        return start_ts, end_ts

    def _session_days_in_range(self, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> list[date]:
        available = set(self.sessions())
        if not available:
            return []
        start_day = start_ts.tz_convert(BRASILIA_TZ).date()
        end_day = end_ts.tz_convert(BRASILIA_TZ).date()
        return [day for day in sorted(available) if start_day <= day <= end_day]

    def ticks(
        self,
        *,
        start: str | datetime,
        end: str | datetime | None = None,
    ) -> pd.DataFrame:
        start_ts, end_ts = self._resolve_read_bounds(start, end)
        days = self._session_days_in_range(start_ts, end_ts)
        frames: list[pd.DataFrame] = []
        for day in days:
            path = self._day_path(day)
            arrays = _read_day_columnar(path)
            frame = _ticks_frame_from_columnar(arrays)
            frames.append(frame)
        if not frames:
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe="ticks",
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
            )
        combined = pd.concat(frames).sort_index(kind="stable")
        combined = filter_index_range(combined, start_ts, end_ts)
        if combined.empty:
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe="ticks",
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
            )
        combined.attrs["q_research"] = {
            "symbol": self._symbol,
            "timeframe": "ticks",
            "source": TICK_STORE_SOURCE,
            "requested_start": start_ts.isoformat(),
            "requested_end": end_ts.isoformat(),
            "returned_start": combined.index[0].isoformat(),
            "returned_end": combined.index[-1].isoformat(),
            "timezone": PUBLIC_TZ_NAME,
        }
        return combined

    def _load_m1_session(self, day: date, *, arrays: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
        cache_path = self._m1_cache_path(day)
        if cache_path.is_file():
            frame = pd.read_parquet(cache_path)
            if frame.index.name != "time":
                frame = frame.set_index("time")
            if frame.index.tz is None:
                frame.index = frame.index.tz_localize(BRASILIA_TZ)
            else:
                frame.index = frame.index.tz_convert(BRASILIA_TZ)
            return frame.sort_index()
        if arrays is None:
            arrays = _read_day_columnar(self._day_path(day))
        m1 = _m1_from_columnar(arrays)
        if not m1.empty:
            validate_bars_frame(m1)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            m1.to_parquet(cache_path, compression="zstd")
        return m1

    def bars(
        self,
        timeframe: str,
        *,
        start: str | datetime,
        end: str | datetime | None = None,
        sync: bool = False,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
    ) -> pd.DataFrame:
        tf = normalize_timeframe(timeframe)
        if tf in ("W1", "MN1"):
            raise ValueError(f"Unsupported timeframe '{timeframe}' for tick store bars")
        if sync:
            self.sync(
                start=start,
                end=end,
                gateway_url=gateway_url,
                gateway_token=gateway_token,
            )
        start_ts, end_ts = self._resolve_read_bounds(start, end)
        days = self._session_days_in_range(start_ts, end_ts)
        if not days:
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe=tf,
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
                hint=_SYNC_HINT,
            )
        session_frames: list[pd.DataFrame] = []
        for day in days:
            m1 = self._load_m1_session(day)
            if m1.empty:
                continue
            session_frames.append(_aggregate_bars(m1, tf))
        if not session_frames:
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe=tf,
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
                hint=_SYNC_HINT,
            )
        frame = pd.concat(session_frames).sort_index(kind="stable")
        frame = filter_index_range(frame, start_ts, end_ts)
        if frame.empty:
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe=tf,
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
                hint=_SYNC_HINT,
            )
        if not frame.empty:
            validate_bars_frame(frame)
        return attach_metadata(
            frame,
            symbol=self._symbol,
            timeframe=tf,
            source=TICK_STORE_SOURCE,
            requested_start=start_ts,
            requested_end=end_ts,
        )

    def _ensure_trade_prices_session(self, day: date) -> dict[str, np.ndarray]:
        if self._trade_prices_cache_day == day and self._trade_prices_cache is not None:
            return self._trade_prices_cache
        arrays = _read_day_columnar(self._day_path(day))
        self._trade_prices_cache_day = day
        self._trade_prices_cache = arrays
        return arrays

    def trade_prices(self, start: str | datetime, end: str | datetime) -> tuple[np.ndarray, np.ndarray]:
        start_ts = parse_query_bound(start)
        end_ts = parse_query_bound(end)
        if start_ts >= end_ts:
            raise ValueError("start must be < end for trade_prices half-open interval")
        day = start_ts.tz_convert(BRASILIA_TZ).date()
        if day not in self.sessions():
            raise NoMarketDataError(
                symbol=self._symbol,
                timeframe="ticks",
                source=TICK_STORE_SOURCE,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
            )
        arrays = self._ensure_trade_prices_session(day)
        from q_backend.market_data.clients.shared import _time_msc_to_naive_local

        times: list[int] = []
        prices: list[float] = []
        start_naive, end_naive = bounds_to_brasilia_naive(start_ts, end_ts)
        for time_msc, last in zip(arrays["time_msc"], arrays["last"], strict=True):
            if not (np.isfinite(last) and last > 0):
                continue
            tick_dt = _time_msc_to_naive_local(int(time_msc))
            if tick_dt < start_naive or tick_dt >= end_naive:
                continue
            times.append(int(time_msc) * 1000)
            prices.append(float(last))
        return np.asarray(times, dtype=np.int64), np.asarray(prices, dtype=np.float64)


def sync_ticks(
    symbol: str,
    start: str | datetime,
    end: str | datetime | None = None,
    *,
    root: str | Path | None = None,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> TickSyncReport:
    """Fetch missing weekday sessions for ``symbol`` into the research tick store."""
    return TickStore(symbol, root=root).sync(
        start=start,
        end=end,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
    )
