"""DuckDB-based range reads over catalogued Parquet lake datasets (Q-020)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import logging
import os
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from q_backend.market_data.clients.metatrader import _empty_ticks_columnar
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP

logger = logging.getLogger(__name__)

PATTERN_CHARACTERS: frozenset[str] = frozenset("*?[]")


class LakePathError(ValueError):
    """A path handed to the query engine is relative or contains a pattern character."""


BAR_COLUMNS: tuple[str, ...] = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)

_instance: duckdb.DuckDBPyConnection | None = None
_instance_pid: int | None = None
_inherited_instances: list[duckdb.DuckDBPyConnection] = []


def _get_instance() -> duckdb.DuckDBPyConnection:
    global _instance, _instance_pid
    current_pid = os.getpid()
    if _instance is None or _instance_pid != current_pid:
        if _instance is not None:
            if _instance_pid == current_pid:
                try:
                    _instance.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to close DuckDB instance: %s", exc)
            else:
                # Retain reference across fork so the parent's C++ connection destructor
                # is not invoked in the child process, which would deadlock on parent mutexes.
                _inherited_instances.append(_instance)
        _instance = duckdb.connect(
            config={
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            }
        )
        _instance.execute("SET GLOBAL TimeZone = 'UTC'")
        _instance_pid = current_pid
    return _instance


def _reset_instance() -> None:
    """Reset the module-level DuckDB instance (used in tests)."""
    global _instance, _instance_pid
    current_pid = os.getpid()
    if _instance is not None and _instance_pid == current_pid:
        try:
            _instance.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to close DuckDB instance: %s", exc)
    _instance = None
    _instance_pid = None


def query_cursor() -> duckdb.DuckDBPyConnection:
    """New cursor on this process's in-memory DuckDB instance (created lazily, recreated if os.getpid() changed)."""
    return _get_instance().cursor()


def _validate_paths(paths: Sequence[Path]) -> list[str]:
    validated: list[str] = []
    for p in paths:
        p_str = str(p)
        if not p.is_absolute():
            raise LakePathError(f"Path must be absolute: {p}")
        if any(c in PATTERN_CHARACTERS for c in p_str):
            raise LakePathError(f"Path contains pattern character: {p}")
        validated.append(str(p.resolve()))
    return validated


def read_bars_table(paths: Sequence[Path], start: datetime, end: datetime) -> pa.Table:
    """Read bars as a PyArrow Table filtered by time window.

    start/end: naive, already normalized by the caller.
    Columns BAR_COLUMNS, time as timestamp[us] naive.
    Filter start <= time <= end inside the scan;
    ORDER BY time, listed-file position, file row number.
    Empty `paths` -> empty table with BAR_COLUMNS and no cursor opened.
    """
    path_strs = _validate_paths(paths)
    if not path_strs:
        schema = pa.schema(
            [
                ("time", pa.timestamp("us")),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("tick_volume", pa.int64()),
                ("spread", pa.float64()),
                ("real_volume", pa.float64()),
            ]
        )
        return pa.table({col: pa.array([], type=schema.field(col).type) for col in BAR_COLUMNS}, schema=schema)

    cur = query_cursor()
    query = """
    SELECT
        CAST(time AS TIMESTAMP) AS time,
        open,
        high,
        low,
        close,
        tick_volume,
        spread,
        real_volume
    FROM read_parquet(?, union_by_name=true, filename=true, file_row_number=true)
    WHERE CAST(time AS TIMESTAMP) BETWEEN ? AND ?
    ORDER BY time, list_position(?, filename), file_row_number
    """
    return cur.execute(query, [path_strs, start, end, path_strs]).to_arrow_table()


def read_ticks_arrays(paths: Sequence[Path], start_msc: int, end_msc: int) -> dict[str, np.ndarray]:
    """Read ticks as a dictionary of columnar NumPy arrays.

    Keys COLUMNAR_TICK_KEYS, dtypes _TICK_DTYPE_MAP.
    Filter start_msc <= time_msc <= end_msc inside the scan;
    ORDER BY time_msc, listed-file position, file row number.
    Raises ValueError naming the first file whose top-level column set != COLUMNAR_TICK_KEYS.
    Empty `paths` -> _empty_ticks_columnar().
    """
    path_strs = _validate_paths(paths)
    if not path_strs:
        return _empty_ticks_columnar()

    cur = query_cursor()

    # Verify column set matches COLUMNAR_TICK_KEYS from parquet footer metadata only
    schema_query = """
    SELECT file_name, name
    FROM parquet_schema(?)
    WHERE name != 'schema'
    """
    schema_rows = cur.execute(schema_query, [path_strs]).fetchall()
    file_cols: dict[str, set[str]] = {}
    for fn, col_name in schema_rows:
        file_cols.setdefault(fn, set()).add(col_name)

    expected_cols = set(COLUMNAR_TICK_KEYS)
    for p_str in path_strs:
        actual_cols = file_cols.get(p_str, set())
        if actual_cols != expected_cols:
            raise ValueError(f"Unexpected tick columns in {p_str}")

    query = """
    SELECT
        time_msc,
        bid,
        ask,
        last,
        volume,
        flags
    FROM read_parquet(?, filename=true, file_row_number=true)
    WHERE time_msc BETWEEN ? AND ?
    ORDER BY time_msc, list_position(?, filename), file_row_number
    """
    table = cur.execute(query, [path_strs, start_msc, end_msc, path_strs]).to_arrow_table()
    if table.num_rows == 0:
        return _empty_ticks_columnar()

    result: dict[str, np.ndarray] = {}
    for col in COLUMNAR_TICK_KEYS:
        result[col] = table[col].to_numpy(zero_copy_only=False).astype(_TICK_DTYPE_MAP[col], copy=False)
    return result
