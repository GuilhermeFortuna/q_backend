import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

COLUMNAR_TICK_KEYS = ("time_msc", "bid", "ask", "last", "volume", "flags")

_TICK_DTYPE_MAP = {
    "time_msc": np.int64,
    "bid": np.float64,
    "ask": np.float64,
    "last": np.float64,
    "volume": np.float64,
    "flags": np.int32,
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def cache_dir() -> Path:
    explicit = os.getenv("Q_TICK_CACHE_DIR")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = _project_root() / path
    else:
        path = _project_root() / "data" / "tick_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug_symbol(symbol: str) -> str:
    return re.sub(r"[^\w.$-]+", "_", symbol)


def make_cache_key(symbol: str, start: datetime, end: datetime, flags: int) -> str:
    payload = f"{symbol}|{start.isoformat()}|{end.isoformat()}|{flags}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    slug = _slug_symbol(symbol)
    return f"{slug}_{digest}"


def _cache_path(key: str) -> Path:
    return cache_dir() / f"{key}.parquet"


def load(key: str) -> dict[str, np.ndarray] | None:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        table = pq.read_table(path)
        if set(table.column_names) != set(COLUMNAR_TICK_KEYS):
            logger.warning("Tick cache %s has unexpected columns", path)
            return None
        result: dict[str, np.ndarray] = {}
        for col in COLUMNAR_TICK_KEYS:
            arr = table[col].to_numpy(zero_copy_only=False)
            result[col] = arr.astype(_TICK_DTYPE_MAP[col], copy=False)
        return result
    except Exception as exc:
        logger.warning("Failed to load tick cache %s: %s", path, exc)
        return None


def store(key: str, arrays: dict[str, np.ndarray]) -> None:
    data = {col: arrays[col] for col in COLUMNAR_TICK_KEYS}
    table = pa.table(data)
    cache_dir().mkdir(parents=True, exist_ok=True)
    pq.write_table(table, _cache_path(key))
