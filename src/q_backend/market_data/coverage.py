"""Coverage planning for OHLCV reads (WO188).

Envelope semantics: local parquet's ``available_range`` defines what is already
on disk. Internal session gaps (weekends, holidays) inside that envelope are
trusted — no bar-count heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from q_backend.market_data import local_store


@dataclass(frozen=True)
class CoveragePlan:
    """How to satisfy an OHLCV read in ``auto`` mode when the remote gateway wins."""

    serve_from: Literal["local", "provider"]
    missing: list[tuple[datetime, datetime]]


def envelope_covers(symbol: str, timeframe: str, start: datetime, end: datetime) -> bool:
    """True when the local store envelope fully contains ``[start, end]``."""
    local = local_store.available_range(symbol, timeframe)
    if local is None:
        return False
    return local.start <= start and local.end >= end


def plan_ohlcv_read(symbol: str, timeframe: str, start: datetime, end: datetime) -> CoveragePlan:
    """Plan an OHLCV read against the local parquet envelope.

    Returns up to two missing segments for the remote gateway to fill:
    head ``[start, local.start)`` and/or tail ``(local.end, end]``.
    """
    local = local_store.available_range(symbol, timeframe)
    if local is None:
        return CoveragePlan(serve_from="provider", missing=[(start, end)])

    if local.start <= start and local.end >= end:
        return CoveragePlan(serve_from="local", missing=[])

    missing: list[tuple[datetime, datetime]] = []
    if start < local.start:
        missing.append((start, local.start))
    if end > local.end:
        missing.append((local.end, end))

    return CoveragePlan(serve_from="provider", missing=missing)
