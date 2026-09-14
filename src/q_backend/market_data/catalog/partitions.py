"""Lake partition definitions and immutable writing helpers."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from q_backend.market_data.clients.shared import _time_msc_to_naive_local


@dataclass(frozen=True)
class Subject:
    kind: Literal["bars", "ticks"]
    symbol: str
    timeframe: str  # "" for ticks


@dataclass(frozen=True)
class WrittenFile:
    path: str  # root-relative
    size_bytes: int
    checksum: str
    rows: int
    partition_start: datetime  # naive Brasília, from the data
    partition_end: datetime
    arrow_schema: dict[str, Any]


def _slug_symbol(symbol: str) -> str:
    return re.sub(r"[^\w.$-]+", "_", symbol)


def _normalize_arrow_type(type_str: str) -> str:
    type_str = type_str.lower()
    if type_str in ("double", "float64", "f8"):
        return "float64"
    if type_str in ("int64", "i8"):
        return "int64"
    if type_str in ("int32", "i4"):
        return "int32"
    if type_str in ("uint32", "u4"):
        return "uint32"
    if type_str in ("float32", "float", "f4"):
        return "float32"
    if type_str.startswith("timestamp"):
        return type_str
    return type_str


def lake_arrow_schema(schema: pa.Schema, name: str = "bars") -> dict[str, Any]:
    """Derive catalog manifest arrow_schema definition from PyArrow schema."""
    fields: list[dict[str, Any]] = []
    for field in schema:
        norm_type = _normalize_arrow_type(str(field.type))
        decl: dict[str, Any] = {
            "name": field.name,
            "type": norm_type,
            "nullable": field.nullable,
        }
        if norm_type.startswith("timestamp"):
            field_tz = getattr(field.type, "tz", None)
            decl["tz"] = str(field_tz) if field_tz else "naive-wallclock-America/Sao_Paulo"
        fields.append(decl)
    return {
        "name": name,
        "fields": fields,
    }


def _extract_partition_bounds(table: pa.Table, kind: str) -> tuple[datetime, datetime]:
    if len(table) == 0:
        epoch = datetime(1970, 1, 1)
        return epoch, epoch

    if kind == "ticks" or "time_msc" in table.column_names:
        col = table["time_msc"]
        min_msc = int(pc.min(col).as_py())
        max_msc = int(pc.max(col).as_py())
        return _time_msc_to_naive_local(min_msc), _time_msc_to_naive_local(max_msc)

    col = table["time"]
    p_min = pc.min(col).as_py()
    p_max = pc.max(col).as_py()
    if isinstance(p_min, datetime):
        dt_min = p_min.replace(tzinfo=None) if p_min.tzinfo else p_min
    else:
        dt_min = pd.Timestamp(p_min).to_pydatetime().replace(tzinfo=None)
    if isinstance(p_max, datetime):
        dt_max = p_max.replace(tzinfo=None) if p_max.tzinfo else p_max
    else:
        dt_max = pd.Timestamp(p_max).to_pydatetime().replace(tzinfo=None)
    return dt_min, dt_max


def write_partition_immutable(root: Path, subject: Subject, partition_key: str, table: pa.Table) -> WrittenFile:
    """Temp file in the target directory, fsync, content-addressed rename, fsync the directory."""
    if subject.kind == "bars":
        target_dir = root / "ohlcv" / _slug_symbol(subject.symbol) / subject.timeframe.upper()
    else:
        target_dir = root / "ticks" / _slug_symbol(subject.symbol)
    target_dir.mkdir(parents=True, exist_ok=True)

    temp_file = target_dir / f".tmp_{partition_key}_{uuid.uuid4().hex}.parquet"
    try:
        pq.write_table(table, temp_file)
        with open(temp_file, "r+b") as f:
            f.flush()
            os.fsync(f.fileno())

        hasher = hashlib.sha256()
        with open(temp_file, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        checksum = hasher.hexdigest()
        size_bytes = temp_file.stat().st_size

        final_name = f"{partition_key}.{checksum[:16]}.parquet"
        final_path = target_dir / final_name
        os.replace(temp_file, final_path)

        # Fsync directory to ensure rename durability
        dir_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_file.exists():
            temp_file.unlink()

    rel_path = final_path.relative_to(root).as_posix()
    p_start, p_end = _extract_partition_bounds(table, subject.kind)
    arrow_schema = lake_arrow_schema(table.schema, name=subject.kind)

    return WrittenFile(
        path=rel_path,
        size_bytes=size_bytes,
        checksum=checksum,
        rows=len(table),
        partition_start=p_start,
        partition_end=p_end,
        arrow_schema=arrow_schema,
    )


def describe_existing_partition(root: Path, rel_path: str, kind: Literal["bars", "ticks"] | None = None) -> WrittenFile:
    """Compute checksum, size, bounds, and schema for an existing partition file."""
    abs_path = root / rel_path
    if not abs_path.is_file():
        raise FileNotFoundError(f"Partition file not found: {abs_path}")

    hasher = hashlib.sha256()
    with open(abs_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    checksum = hasher.hexdigest()
    size_bytes = abs_path.stat().st_size

    table = pq.read_table(abs_path)
    if kind is None:
        if rel_path.startswith("ohlcv/"):
            kind = "bars"
        elif rel_path.startswith("ticks/"):
            kind = "ticks"
        elif "time_msc" in table.column_names:
            kind = "ticks"
        else:
            kind = "bars"

    p_start, p_end = _extract_partition_bounds(table, kind)
    arrow_schema = lake_arrow_schema(table.schema, name=kind)

    return WrittenFile(
        path=Path(rel_path).as_posix(),
        size_bytes=size_bytes,
        checksum=checksum,
        rows=len(table),
        partition_start=p_start,
        partition_end=p_end,
        arrow_schema=arrow_schema,
    )
