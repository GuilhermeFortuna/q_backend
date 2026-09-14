"""Lake partition definitions and immutable writing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa


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
