"""Portable local OHLCV and tick parquet store (WO48 / WO50 / Q-017).

Delegates storage, publication, and resolution to the database-backed LakeCatalog.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, list_current_datasets
from q_backend.market_data.catalog.service import get_lake_catalog
from q_backend.market_data.lake_query import read_bars_table, read_ticks_arrays
from q_backend.market_data.clients.metatrader import (
    OhlcvAvailableRange,
    _empty_ticks_columnar,
    _naive_local_to_time_msc,
)
from q_backend.market_data.models import OHLCV
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.storage.db.catalog_models import Dataset
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)

_KIND_BARS = "bars"
_KIND_TICKS = "ticks"

_OHLCV_COLUMNS = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def market_data_root() -> Path:
    settings = get_settings()
    explicit = os.getenv("Q_MARKET_DATA_ROOT")
    if explicit:
        root = Path(explicit)
        if not root.is_absolute():
            root = _project_root() / root
    else:
        root = Path(settings.market_data_root)
        if not root.is_absolute():
            root = _project_root() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slug_symbol(symbol: str) -> str:
    return re.sub(r"[^\w.$-]+", "_", symbol)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_brasilia_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRASILIA_TZ).replace(tzinfo=None).isoformat()


def _to_brasilia_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRASILIA_TZ).replace(tzinfo=None)


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _dataset_to_inventory_entry(d: Dataset) -> dict[str, Any]:
    total_bytes = sum(f.size_bytes for f in d.files)
    entry: dict[str, Any] = {
        "symbol": d.symbol,
        "kind": d.kind,
        "start": _to_brasilia_iso(d.time_start),
        "end": _to_brasilia_iso(d.time_end),
        "rows": d.row_count,
        "bytes": total_bytes,
        "updated_at": _to_utc_iso(d.published_at),
    }
    if d.kind == _KIND_BARS:
        entry["timeframe"] = d.timeframe.upper()
    return entry


def _bars_to_dataframe(bars: list[OHLCV]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=list(_OHLCV_COLUMNS))
    df = pd.DataFrame([bar.model_dump() for bar in bars])
    df["time"] = pd.to_datetime(df["time"])
    return df


def _table_to_bars(table: pa.Table) -> list[OHLCV]:
    if table.num_rows == 0:
        return []
    time_col = table["time"].to_pylist()
    open_col = table["open"].to_pylist()
    high_col = table["high"].to_pylist()
    low_col = table["low"].to_pylist()
    close_col = table["close"].to_pylist()
    tick_volume_col = table["tick_volume"].to_pylist()
    spread_col = table["spread"].to_pylist()
    real_volume_col = table["real_volume"].to_pylist()

    bars: list[OHLCV] = []
    for t, o, h, l, c, tv, sp, rv in zip(
        time_col,
        open_col,
        high_col,
        low_col,
        close_col,
        tick_volume_col,
        spread_col,
        real_volume_col,
    ):
        bars.append(
            OHLCV(
                time=t,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                tick_volume=int(tv),
                spread=int(sp) if sp is not None else None,
                real_volume=int(rv) if rv is not None else None,
            )
        )
    return bars


def write_ohlcv(symbol: str, timeframe: str, bars: list[OHLCV]) -> dict[str, Any]:
    """Publish bars into year-partitioned parquet files and record in the catalog."""
    tf = timeframe.upper()
    catalog = get_lake_catalog()

    if not bars:
        with catalog.session_factory() as session:
            dataset = current_dataset(session, Subject(kind=_KIND_BARS, symbol=symbol, timeframe=tf))
            if dataset is not None:
                return _dataset_to_inventory_entry(dataset)
        return {
            "symbol": symbol,
            "kind": _KIND_BARS,
            "timeframe": tf,
            "start": None,
            "end": None,
            "rows": 0,
            "bytes": 0,
            "updated_at": _utc_now_iso(),
        }

    df = _bars_to_dataframe(bars)
    dataset = catalog.publish_bars(symbol, tf, df)
    return _dataset_to_inventory_entry(dataset)


def write_ticks(symbol: str, arrays: dict[str, np.ndarray]) -> dict[str, Any]:
    """Publish columnar ticks into month-partitioned parquet and record in the catalog."""
    catalog = get_lake_catalog()
    if len(arrays.get("time_msc", [])) == 0:
        with catalog.session_factory() as session:
            dataset = current_dataset(session, Subject(kind=_KIND_TICKS, symbol=symbol, timeframe=""))
            if dataset is not None:
                return _dataset_to_inventory_entry(dataset)
        return {
            "symbol": symbol,
            "kind": _KIND_TICKS,
            "start": None,
            "end": None,
            "rows": 0,
            "bytes": 0,
            "updated_at": _utc_now_iso(),
        }

    dataset = catalog.publish_ticks(symbol, arrays)
    return _dataset_to_inventory_entry(dataset)


def _to_naive_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Drop tz info, normalizing to UTC, so bounds match the naive store."""
    if ts.tz is not None:
        return ts.tz_convert("UTC").tz_localize(None)
    return ts


def read_ohlcv(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]:
    """
    Read OHLCV bars from catalog-addressed parquet files.

    The stored `time` column is tz-naive Brasília wall-clock; callers may pass
    either tz-naive or tz-aware bounds. Bound comparisons normalize to naive UTC/local
    so comparisons never raise tz-naive/tz-aware TypeErrors.
    """
    tf = timeframe.upper()
    start_ts = _to_naive_utc(pd.Timestamp(start))
    end_ts = _to_naive_utc(pd.Timestamp(end))
    if start_ts > end_ts:
        return []

    catalog = get_lake_catalog()
    subject = Subject(kind=_KIND_BARS, symbol=symbol, timeframe=tf)
    paths = catalog.files_for_range(subject, start, end)
    if not paths:
        return []

    existing_paths = [path for path in paths if path.is_file()]
    if not existing_paths:
        return []

    table = read_bars_table(existing_paths, start_ts.to_pydatetime(), end_ts.to_pydatetime())
    return _table_to_bars(table)


def read_ticks_columnar(symbol: str, start: datetime, end: datetime) -> dict[str, np.ndarray]:
    """Read ticks from catalog-addressed month parquet files."""
    start_msc = _naive_local_to_time_msc(start)
    end_msc = _naive_local_to_time_msc(end)
    if start_msc > end_msc:
        return _empty_ticks_columnar()

    catalog = get_lake_catalog()
    subject = Subject(kind=_KIND_TICKS, symbol=symbol, timeframe="")
    paths = catalog.files_for_range(subject, start, end)
    if not paths:
        return _empty_ticks_columnar()

    existing_paths = [path for path in paths if path.is_file()]
    if not existing_paths:
        return _empty_ticks_columnar()

    return read_ticks_arrays(existing_paths, start_msc, end_msc)


def delete_ohlcv(symbol: str, timeframe: str) -> None:
    """Tombstone the specified bars dataset in the catalog (files are retained for grace period)."""
    catalog = get_lake_catalog()
    catalog.delete_subject(Subject(kind=_KIND_BARS, symbol=symbol, timeframe=timeframe.upper()))


def delete_ticks(symbol: str) -> None:
    """Tombstone the specified ticks dataset in the catalog (files are retained for grace period)."""
    catalog = get_lake_catalog()
    catalog.delete_subject(Subject(kind=_KIND_TICKS, symbol=symbol, timeframe=""))


def list_inventory() -> list[dict[str, Any]]:
    """Return list of inventory items representing all currently published datasets."""
    catalog = get_lake_catalog()
    with catalog.session_factory() as session:
        datasets = list_current_datasets(session)
        return [_dataset_to_inventory_entry(d) for d in datasets]


def available_range(symbol: str, timeframe: str) -> OhlcvAvailableRange | None:
    """Return available datetime range and bar count for a bars series from the catalog."""
    tf = timeframe.upper()
    catalog = get_lake_catalog()
    with catalog.session_factory() as session:
        dataset = current_dataset(session, Subject(kind=_KIND_BARS, symbol=symbol, timeframe=tf))
        if dataset is None:
            return None
        return OhlcvAvailableRange(
            symbol=symbol,
            timeframe=tf,
            start=_to_brasilia_naive(dataset.time_start),
            end=_to_brasilia_naive(dataset.time_end),
            bar_count=int(dataset.row_count),
        )


def tick_available_range(symbol: str) -> dict[str, Any] | None:
    """Return available range dictionary for a ticks series from the catalog."""
    catalog = get_lake_catalog()
    with catalog.session_factory() as session:
        dataset = current_dataset(session, Subject(kind=_KIND_TICKS, symbol=symbol, timeframe=""))
        if dataset is None:
            return None
        return _dataset_to_inventory_entry(dataset)


def stored_symbols() -> list[dict[str, Any]]:
    symbols: dict[str, dict[str, Any]] = {}
    for entry in list_inventory():
        sym = entry["symbol"]
        if sym not in symbols:
            symbols[sym] = {
                "name": sym,
                "description": sym,
                "path": f"LOCAL\\{sym}",
                "custom": False,
            }
    return list(symbols.values())


def inventory_count() -> int:
    return len(list_inventory())
