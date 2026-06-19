"""Portable local OHLCV and tick parquet store (WO48 / WO50).

Layout under ``market_data_root``::

    ohlcv/{symbol_slug}/{timeframe}/{YYYY}.parquet
    ticks/{symbol_slug}/{YYYY-MM}.parquet
    catalog.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from q_backend.market_data.clients.metatrader import (
    OhlcvAvailableRange,
    _empty_ticks_columnar,
    _naive_local_to_time_msc,
    _time_msc_to_naive_local,
)
from q_backend.market_data.models import OHLCV
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP
from q_backend.market_data.timezone import to_brasilia_naive
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


def _catalog_path() -> Path:
    return market_data_root() / "catalog.json"


def _ohlcv_series_dir(symbol: str, timeframe: str) -> Path:
    return (
        market_data_root()
        / "ohlcv"
        / _slug_symbol(symbol)
        / timeframe.upper()
    )


def _year_parquet_path(symbol: str, timeframe: str, year: int) -> Path:
    return _ohlcv_series_dir(symbol, timeframe) / f"{year}.parquet"


def _ticks_series_dir(symbol: str) -> Path:
    return market_data_root() / "ticks" / _slug_symbol(symbol)


def _month_parquet_path(symbol: str, month_key: str) -> Path:
    return _ticks_series_dir(symbol) / f"{month_key}.parquet"


def _normalize_catalog_entry(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    kind = normalized.get("kind")
    if kind not in (_KIND_BARS, _KIND_TICKS):
        normalized["kind"] = _KIND_BARS if normalized.get("timeframe") else _KIND_TICKS
    return normalized


def _catalog_entry_key(row: dict[str, Any]) -> tuple[str, str, str]:
    entry = _normalize_catalog_entry(row)
    kind = str(entry["kind"])
    symbol = str(entry.get("symbol", ""))
    if kind == _KIND_TICKS:
        return symbol, kind, ""
    timeframe = str(entry.get("timeframe", "")).upper()
    return symbol, kind, timeframe


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_catalog() -> list[dict[str, Any]]:
    path = _catalog_path()
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [_normalize_catalog_entry(row) for row in data]
        logger.warning("Catalog %s is not a JSON array", path)
        return []
    except Exception as exc:
        logger.warning("Failed to read catalog %s: %s", path, exc)
        return []


def _write_catalog(entries: list[dict[str, Any]]) -> None:
    path = _catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _bars_to_dataframe(bars: list[OHLCV]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=list(_OHLCV_COLUMNS))
    df = pd.DataFrame([bar.model_dump() for bar in bars])
    df["time"] = pd.to_datetime(df["time"])
    return df


def _dataframe_to_bars(df: pd.DataFrame) -> list[OHLCV]:
    if df.empty:
        return []
    ordered = df.sort_values("time")
    bars: list[OHLCV] = []
    for row in ordered.itertuples(index=False):
        spread = row.spread
        real_volume = row.real_volume
        bars.append(
            OHLCV(
                time=pd.Timestamp(row.time).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                tick_volume=int(row.tick_volume),
                spread=int(spread) if pd.notna(spread) else None,
                real_volume=int(real_volume) if pd.notna(real_volume) else None,
            )
        )
    return bars


def _merge_year_frame(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        combined = incoming.copy()
    elif incoming.empty:
        combined = existing.copy()
    else:
        combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return combined
    combined["time"] = pd.to_datetime(combined["time"])
    combined = combined.sort_values("time")
    combined = combined.drop_duplicates(subset=["time"], keep="last")
    return combined.reset_index(drop=True)


def _write_year_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df[list(_OHLCV_COLUMNS)], preserve_index=False)
    pq.write_table(table, path)


def _read_year_parquet(path: Path) -> pd.DataFrame:
    table = pq.read_table(path)
    df = table.to_pandas()
    df["time"] = pd.to_datetime(df["time"])
    return df


def _summarize_series(symbol: str, timeframe: str) -> dict[str, Any] | None:
    series_dir = _ohlcv_series_dir(symbol, timeframe)
    if not series_dir.is_dir():
        return None
    year_files = sorted(series_dir.glob("*.parquet"))
    if not year_files:
        return None

    total_rows = 0
    total_bytes = 0
    min_time: pd.Timestamp | None = None
    max_time: pd.Timestamp | None = None

    for year_file in year_files:
        total_bytes += year_file.stat().st_size
        df = _read_year_parquet(year_file)
        if df.empty:
            continue
        total_rows += len(df)
        file_min = pd.Timestamp(df["time"].min())
        file_max = pd.Timestamp(df["time"].max())
        min_time = file_min if min_time is None else min(min_time, file_min)
        max_time = file_max if max_time is None else max(max_time, file_max)

    if total_rows == 0 or min_time is None or max_time is None:
        return None

    return {
        "symbol": symbol,
        "kind": _KIND_BARS,
        "timeframe": timeframe.upper(),
        "start": min_time.isoformat(),
        "end": max_time.isoformat(),
        "rows": total_rows,
        "bytes": total_bytes,
        "updated_at": _utc_now_iso(),
    }


def _upsert_catalog_entry(entry: dict[str, Any]) -> None:
    entry = _normalize_catalog_entry(entry)
    catalog = _read_catalog()
    key = _catalog_entry_key(entry)
    updated = [row for row in catalog if _catalog_entry_key(row) != key]
    updated.append(entry)
    updated.sort(
        key=lambda row: (
            row.get("symbol", ""),
            row.get("kind", _KIND_BARS),
            row.get("timeframe", ""),
        )
    )
    _write_catalog(updated)


def _remove_catalog_entry(symbol: str, kind: str, timeframe: str = "") -> None:
    catalog = _read_catalog()
    tf = timeframe.upper()
    updated = [
        row
        for row in catalog
        if not (
            row.get("symbol") == symbol
            and row.get("kind", _KIND_BARS) == kind
            and (
                kind == _KIND_TICKS
                or str(row.get("timeframe", "")).upper() == tf
            )
        )
    ]
    _write_catalog(updated)


def write_ohlcv(symbol: str, timeframe: str, bars: list[OHLCV]) -> dict[str, Any]:
    """Merge bars into year-partitioned parquet files and refresh the catalog entry."""
    tf = timeframe.upper()
    if not bars:
        entry = _summarize_series(symbol, tf)
        if entry is None:
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
        _upsert_catalog_entry(entry)
        return entry

    df = _bars_to_dataframe(bars)
    for year, year_df in df.groupby(df["time"].dt.year):
        year_int = int(year)
        path = _year_parquet_path(symbol, tf, year_int)
        existing = _read_year_parquet(path) if path.is_file() else None
        merged = _merge_year_frame(existing, year_df)
        _write_year_parquet(path, merged)

    entry = _summarize_series(symbol, tf)
    if entry is None:
        raise RuntimeError(f"Failed to summarize OHLCV series for {symbol}/{tf}")
    _upsert_catalog_entry(entry)
    return entry


def read_ohlcv(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[OHLCV]:
    tf = timeframe.upper()
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts > end_ts:
        return []

    years = range(start_ts.year, end_ts.year + 1)
    frames: list[pd.DataFrame] = []
    for year in years:
        path = _year_parquet_path(symbol, tf, year)
        if path.is_file():
            frames.append(_read_year_parquet(path))

    if not frames:
        return []

    combined = pd.concat(frames, ignore_index=True)
    combined["time"] = pd.to_datetime(combined["time"])
    mask = (combined["time"] >= start_ts) & (combined["time"] <= end_ts)
    filtered = combined.loc[mask].sort_values("time")
    return _dataframe_to_bars(filtered)


def _arrays_to_dataframe(arrays: dict[str, np.ndarray]) -> pd.DataFrame:
    if len(arrays.get("time_msc", [])) == 0:
        return pd.DataFrame(columns=list(COLUMNAR_TICK_KEYS))
    return pd.DataFrame({col: arrays[col] for col in COLUMNAR_TICK_KEYS})


def _dataframe_to_columnar(df: pd.DataFrame) -> dict[str, np.ndarray]:
    if df.empty:
        return _empty_ticks_columnar()
    ordered = df.sort_values("time_msc")
    result: dict[str, np.ndarray] = {}
    for col in COLUMNAR_TICK_KEYS:
        result[col] = ordered[col].to_numpy(dtype=_TICK_DTYPE_MAP[col], copy=False)
    return result


def _merge_tick_frame(
    existing: pd.DataFrame | None, incoming: pd.DataFrame
) -> pd.DataFrame:
    if existing is None or existing.empty:
        combined = incoming.copy()
    elif incoming.empty:
        combined = existing.copy()
    else:
        combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return combined
    combined = combined.sort_values("time_msc")
    combined = combined.drop_duplicates(subset=["time_msc"], keep="last")
    return combined.reset_index(drop=True)


def _write_month_ticks(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df[list(COLUMNAR_TICK_KEYS)], preserve_index=False)
    pq.write_table(table, path)


def _read_month_ticks(path: Path) -> pd.DataFrame:
    table = pq.read_table(path)
    if set(table.column_names) != set(COLUMNAR_TICK_KEYS):
        raise ValueError(f"Unexpected tick columns in {path}")
    df = table.to_pandas()
    for col in COLUMNAR_TICK_KEYS:
        df[col] = df[col].astype(_TICK_DTYPE_MAP[col], copy=False)
    return df


def _month_key_from_msc(msc: int) -> str:
    dt = _time_msc_to_naive_local(msc)
    return f"{dt.year}-{dt.month:02d}"


def _iter_month_keys(start: datetime, end: datetime) -> list[str]:
    start_local = to_brasilia_naive(start)
    end_local = to_brasilia_naive(end)
    cursor = datetime(start_local.year, start_local.month, 1)
    end_month = datetime(end_local.year, end_local.month, 1)
    keys: list[str] = []
    while cursor <= end_month:
        keys.append(f"{cursor.year}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = datetime(cursor.year + 1, 1, 1)
        else:
            cursor = datetime(cursor.year, cursor.month + 1, 1)
    return keys


def _summarize_ticks(symbol: str) -> dict[str, Any] | None:
    series_dir = _ticks_series_dir(symbol)
    if not series_dir.is_dir():
        return None
    month_files = sorted(series_dir.glob("*.parquet"))
    if not month_files:
        return None

    total_rows = 0
    total_bytes = 0
    min_msc: int | None = None
    max_msc: int | None = None

    for month_file in month_files:
        total_bytes += month_file.stat().st_size
        df = _read_month_ticks(month_file)
        if df.empty:
            continue
        total_rows += len(df)
        file_min = int(df["time_msc"].min())
        file_max = int(df["time_msc"].max())
        min_msc = file_min if min_msc is None else min(min_msc, file_min)
        max_msc = file_max if max_msc is None else max(max_msc, file_max)

    if total_rows == 0 or min_msc is None or max_msc is None:
        return None

    return {
        "symbol": symbol,
        "kind": _KIND_TICKS,
        "start": _time_msc_to_naive_local(min_msc).isoformat(),
        "end": _time_msc_to_naive_local(max_msc).isoformat(),
        "rows": total_rows,
        "bytes": total_bytes,
        "updated_at": _utc_now_iso(),
    }


def write_ticks(symbol: str, arrays: dict[str, np.ndarray]) -> dict[str, Any]:
    """Merge columnar ticks into month-partitioned parquet and refresh catalog."""
    if len(arrays.get("time_msc", [])) == 0:
        entry = _summarize_ticks(symbol)
        if entry is None:
            return {
                "symbol": symbol,
                "kind": _KIND_TICKS,
                "start": None,
                "end": None,
                "rows": 0,
                "bytes": 0,
                "updated_at": _utc_now_iso(),
            }
        _upsert_catalog_entry(entry)
        return entry

    df = _arrays_to_dataframe(arrays)
    df["_month"] = df["time_msc"].map(lambda msc: _month_key_from_msc(int(msc)))
    for month_key, month_df in df.groupby("_month", sort=True):
        path = _month_parquet_path(symbol, str(month_key))
        existing = _read_month_ticks(path) if path.is_file() else None
        merged = _merge_tick_frame(existing, month_df.drop(columns=["_month"]))
        _write_month_ticks(path, merged)

    entry = _summarize_ticks(symbol)
    if entry is None:
        raise RuntimeError(f"Failed to summarize tick series for {symbol}")
    _upsert_catalog_entry(entry)
    return entry


def read_ticks_columnar(
    symbol: str, start: datetime, end: datetime
) -> dict[str, np.ndarray]:
    start_msc = _naive_local_to_time_msc(start)
    end_msc = _naive_local_to_time_msc(end)
    if start_msc > end_msc:
        return _empty_ticks_columnar()

    frames: list[pd.DataFrame] = []
    for month_key in _iter_month_keys(start, end):
        path = _month_parquet_path(symbol, month_key)
        if path.is_file():
            frames.append(_read_month_ticks(path))

    if not frames:
        return _empty_ticks_columnar()

    combined = pd.concat(frames, ignore_index=True)
    mask = (combined["time_msc"] >= start_msc) & (combined["time_msc"] <= end_msc)
    filtered = combined.loc[mask]
    return _dataframe_to_columnar(filtered)


def tick_available_range(symbol: str) -> dict[str, Any] | None:
    catalog = _read_catalog()
    for row in catalog:
        if row.get("symbol") == symbol and row.get("kind") == _KIND_TICKS:
            return row
    return _summarize_ticks(symbol)


def delete_ticks(symbol: str) -> None:
    series_dir = _ticks_series_dir(symbol)
    if series_dir.is_dir():
        shutil.rmtree(series_dir, ignore_errors=True)
    _remove_catalog_entry(symbol, _KIND_TICKS)


def available_range(symbol: str, timeframe: str) -> OhlcvAvailableRange | None:
    tf = timeframe.upper()
    catalog = _read_catalog()
    for row in catalog:
        if (
            row.get("symbol") == symbol
            and row.get("kind", _KIND_BARS) == _KIND_BARS
            and str(row.get("timeframe", "")).upper() == tf
        ):
            start = pd.Timestamp(row["start"]).to_pydatetime()
            end = pd.Timestamp(row["end"]).to_pydatetime()
            return OhlcvAvailableRange(
                symbol=symbol,
                timeframe=tf,
                start=start,
                end=end,
                bar_count=int(row.get("rows", 0)),
            )
    return _summarize_series_as_range(symbol, tf)


def _summarize_series_as_range(symbol: str, timeframe: str) -> OhlcvAvailableRange | None:
    entry = _summarize_series(symbol, timeframe)
    if entry is None:
        return None
    return OhlcvAvailableRange(
        symbol=symbol,
        timeframe=timeframe.upper(),
        start=pd.Timestamp(entry["start"]).to_pydatetime(),
        end=pd.Timestamp(entry["end"]).to_pydatetime(),
        bar_count=int(entry["rows"]),
    )


def list_inventory() -> list[dict[str, Any]]:
    catalog = _read_catalog()
    if catalog:
        return catalog

    entries: list[dict[str, Any]] = []
    ohlcv_root = market_data_root() / "ohlcv"
    if ohlcv_root.is_dir():
        for symbol_dir in sorted(ohlcv_root.iterdir()):
            if not symbol_dir.is_dir():
                continue
            for tf_dir in sorted(symbol_dir.iterdir()):
                if not tf_dir.is_dir():
                    continue
                symbol = symbol_dir.name
                timeframe = tf_dir.name
                entry = _summarize_series(symbol, timeframe)
                if entry is not None:
                    entries.append(entry)

    ticks_root = market_data_root() / "ticks"
    if ticks_root.is_dir():
        for symbol_dir in sorted(ticks_root.iterdir()):
            if not symbol_dir.is_dir():
                continue
            entry = _summarize_ticks(symbol_dir.name)
            if entry is not None:
                entries.append(entry)

    if entries:
        _write_catalog(entries)
    return entries


def delete_ohlcv(symbol: str, timeframe: str) -> None:
    tf = timeframe.upper()
    series_dir = _ohlcv_series_dir(symbol, tf)
    if series_dir.is_dir():
        shutil.rmtree(series_dir, ignore_errors=True)
    _remove_catalog_entry(symbol, _KIND_BARS, tf)


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
