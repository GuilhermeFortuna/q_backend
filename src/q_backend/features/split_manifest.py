"""Immutable three-way split manifest for alpha-research runs (WO162).

Segments (chronological, disjoint):

```text
[ feature evidence ][ walk-forward + repeated seeds ][ untouched lock-box ]
```
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd


DEFAULT_EVIDENCE_FRACTION = 0.40
DEFAULT_WALKFORWARD_FRACTION = 0.40
DEFAULT_LOCKBOX_FRACTION = 0.20


@dataclass(frozen=True)
class SplitSegment:
    name: str
    start: datetime
    end: datetime
    bar_count: int


@dataclass(frozen=True)
class SplitManifest:
    """Versioned, hashable split over a bar range."""

    symbol: str
    timeframe: str
    range_start: datetime
    range_end: datetime
    evidence: SplitSegment
    walkforward: SplitSegment
    lockbox: SplitSegment
    fractions: tuple[float, float, float]
    manifest_hash: str
    data_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "range_start": _iso(self.range_start),
            "range_end": _iso(self.range_end),
            "fractions": list(self.fractions),
            "evidence": _segment_dict(self.evidence),
            "walkforward": _segment_dict(self.walkforward),
            "lockbox": _segment_dict(self.lockbox),
            "manifest_hash": self.manifest_hash,
            "data_fingerprint": self.data_fingerprint,
        }


def _iso(value: datetime) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _segment_dict(segment: SplitSegment) -> dict[str, Any]:
    return {
        "name": segment.name,
        "start": _iso(segment.start),
        "end": _iso(segment.end),
        "bar_count": segment.bar_count,
    }


def compute_data_fingerprint(bars: pd.DataFrame) -> str:
    """Content hash of bar count and UTC time endpoints."""
    if bars.empty:
        payload = {"bar_count": 0}
    else:
        times = pd.to_datetime(bars["time"], utc=True)
        payload = {
            "bar_count": int(len(bars)),
            "start": _iso(times.iloc[0].to_pydatetime()),
            "end": _iso(times.iloc[-1].to_pydatetime()),
        }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _split_edges(n: int, fractions: tuple[float, float, float]) -> tuple[int, int]:
    if n <= 0:
        return 0, 0
    evidence_end = int(n * fractions[0])
    walkforward_end = evidence_end + int(n * fractions[1])
    evidence_end = max(1, min(evidence_end, n - 2))
    walkforward_end = max(evidence_end + 1, min(walkforward_end, n - 1))
    return evidence_end, walkforward_end


def build_split_manifest(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    evidence_fraction: float = DEFAULT_EVIDENCE_FRACTION,
    walkforward_fraction: float = DEFAULT_WALKFORWARD_FRACTION,
    lockbox_fraction: float = DEFAULT_LOCKBOX_FRACTION,
) -> SplitManifest:
    """Build a deterministic three-way split from sorted bars."""
    if bars.empty:
        raise ValueError("bars must not be empty")
    if not {"time", "close"}.issubset(bars.columns):
        raise ValueError("bars must contain time and close columns")

    total = evidence_fraction + walkforward_fraction + lockbox_fraction
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"split fractions must sum to 1.0, got {total}")

    ordered = bars.sort_values("time").reset_index(drop=True)
    times = pd.to_datetime(ordered["time"], utc=True)
    n = len(ordered)
    evidence_end, walkforward_end = _split_edges(
        n, (evidence_fraction, walkforward_fraction, lockbox_fraction)
    )

    evidence = SplitSegment(
        name="evidence",
        start=times.iloc[0].to_pydatetime(),
        end=times.iloc[evidence_end - 1].to_pydatetime(),
        bar_count=evidence_end,
    )
    walkforward = SplitSegment(
        name="walkforward",
        start=times.iloc[evidence_end].to_pydatetime(),
        end=times.iloc[walkforward_end - 1].to_pydatetime(),
        bar_count=walkforward_end - evidence_end,
    )
    lockbox = SplitSegment(
        name="lockbox",
        start=times.iloc[walkforward_end].to_pydatetime(),
        end=times.iloc[-1].to_pydatetime(),
        bar_count=n - walkforward_end,
    )

    fingerprint = compute_data_fingerprint(ordered)
    manifest_body = {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "range_start": _iso(times.iloc[0].to_pydatetime()),
        "range_end": _iso(times.iloc[-1].to_pydatetime()),
        "fractions": [evidence_fraction, walkforward_fraction, lockbox_fraction],
        "evidence": _segment_dict(evidence),
        "walkforward": _segment_dict(walkforward),
        "lockbox": _segment_dict(lockbox),
        "data_fingerprint": fingerprint,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return SplitManifest(
        symbol=symbol,
        timeframe=timeframe.upper(),
        range_start=times.iloc[0].to_pydatetime(),
        range_end=times.iloc[-1].to_pydatetime(),
        evidence=evidence,
        walkforward=walkforward,
        lockbox=lockbox,
        fractions=(evidence_fraction, walkforward_fraction, lockbox_fraction),
        manifest_hash=manifest_hash,
        data_fingerprint=fingerprint,
    )


def slice_segment(bars: pd.DataFrame, segment: SplitSegment) -> pd.DataFrame:
    """Return bars whose timestamps fall within ``segment`` inclusive bounds."""
    ordered = bars.sort_values("time").reset_index(drop=True)
    times = pd.to_datetime(ordered["time"], utc=True)
    start = pd.Timestamp(segment.start)
    end = pd.Timestamp(segment.end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")
    mask = (times >= start) & (times <= end)
    return ordered.loc[mask].reset_index(drop=True)


def assert_segments_disjoint(manifest: SplitManifest) -> None:
    """Raise when segment date ranges overlap."""
    segments = [manifest.evidence, manifest.walkforward, manifest.lockbox]
    for left in segments:
        for right in segments:
            if left.name == right.name:
                continue
            if left.end >= right.start and right.end >= left.start:
                raise ValueError(
                    f"Split segments '{left.name}' and '{right.name}' overlap."
                )
