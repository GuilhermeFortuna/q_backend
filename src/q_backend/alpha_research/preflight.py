"""Data preflight for instrument alpha-research runs (WO164)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from q_backend.features.split_manifest import SplitManifest, build_split_manifest
from q_backend.market_data.read_through import read_ohlcv_fresh
from q_backend.optimization.hypothesis import RESEARCH_PROFILES, InstrumentResearchProfile

PreflightVerdict = Literal["ok", "inconclusive"]

# Align with exogenous preflight continuity guard.
_MAX_GAP_DAYS = 31
_MIN_TOTAL_BARS = 120


@dataclass(frozen=True)
class PreflightResult:
    verdict: PreflightVerdict
    profile: InstrumentResearchProfile | None
    manifest: SplitManifest | None
    bars: pd.DataFrame | None
    reasons: list[str]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "profile_id": self.profile.profile_id if self.profile else None,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "reasons": list(self.reasons),
            "coverage": self.coverage,
        }


def _bars_to_frame(records: list[Any]) -> pd.DataFrame:
    rows = [
        record.model_dump() if hasattr(record, "model_dump") else dict(record)
        for record in records
    ]
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)


def _expected_bar_timedelta(timeframe: str) -> pd.Timedelta:
    tf = timeframe.upper()
    if tf == "M15":
        return pd.Timedelta(minutes=15)
    if tf == "M5":
        return pd.Timedelta(minutes=5)
    if tf == "H1":
        return pd.Timedelta(hours=1)
    if tf == "D1":
        return pd.Timedelta(days=1)
    return pd.Timedelta(hours=1)


def _max_bar_gap_days(bars: pd.DataFrame, timeframe: str) -> float:
    if len(bars) < 2:
        return 0.0
    times = pd.to_datetime(bars["time"], utc=True)
    deltas = times.diff().dropna()
    if deltas.empty:
        return 0.0
    expected = _expected_bar_timedelta(timeframe)
    # Ignore weekends/holidays: only flag gaps much larger than one bar.
    suspicious = deltas[deltas > expected * 4]
    if suspicious.empty:
        return 0.0
    return float(suspicious.max() / pd.Timedelta(days=1))


def run_data_preflight(
    *,
    profile_id: str,
    start: datetime,
    end: datetime,
    profile_version: int | None = None,
) -> PreflightResult:
    """Verify local coverage, continuity, and build the immutable split manifest."""
    profile = RESEARCH_PROFILES.get(profile_id)
    if profile is None:
        return PreflightResult(
            verdict="inconclusive",
            profile=None,
            manifest=None,
            bars=None,
            reasons=[f"Unknown profile_id '{profile_id}'."],
            coverage={},
        )

    if profile_version is not None and profile.version != profile_version:
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=None,
            bars=None,
            reasons=[
                f"Requested profile_version {profile_version} does not match "
                f"active version {profile.version}."
            ],
            coverage={},
        )

    if profile.symbol.upper().startswith("WDO") and profile.timeframe.upper() == "M5":
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=None,
            bars=None,
            reasons=["WDO$ M5 is not an approved research profile until continuity is repaired."],
            coverage={"symbol": profile.symbol, "timeframe": profile.timeframe},
        )

    records = read_ohlcv_fresh(profile.symbol, profile.timeframe, start, end)
    coverage = {
        "symbol": profile.symbol,
        "timeframe": profile.timeframe,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "bar_count": len(records),
    }
    if not records:
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=None,
            bars=None,
            reasons=["No local OHLCV bars found for the requested date range."],
            coverage=coverage,
        )

    bars = _bars_to_frame(records)
    if len(bars) < _MIN_TOTAL_BARS:
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=None,
            bars=bars,
            reasons=[
                f"Insufficient bar count ({len(bars)} < {_MIN_TOTAL_BARS}) for a "
                "three-way research split."
            ],
            coverage=coverage,
        )

    max_gap_days = _max_bar_gap_days(bars, profile.timeframe)
    coverage["max_gap_days"] = max_gap_days
    if max_gap_days > _MAX_GAP_DAYS:
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=None,
            bars=bars,
            reasons=[
                f"Local series continuity break of {max_gap_days:.1f} days exceeds "
                f"{_MAX_GAP_DAYS}-day limit."
            ],
            coverage=coverage,
        )

    manifest = build_split_manifest(
        bars,
        symbol=profile.symbol,
        timeframe=profile.timeframe,
    )
    min_obs = int(profile.evidence_thresholds.get("min_obs", 100))
    if manifest.evidence.bar_count < min_obs:
        return PreflightResult(
            verdict="inconclusive",
            profile=profile,
            manifest=manifest,
            bars=bars,
            reasons=[
                f"Evidence segment has {manifest.evidence.bar_count} bars; "
                f"profile requires at least {min_obs}."
            ],
            coverage={
                **coverage,
                "evidence_bars": manifest.evidence.bar_count,
                "walkforward_bars": manifest.walkforward.bar_count,
                "lockbox_bars": manifest.lockbox.bar_count,
            },
        )

    coverage.update(
        {
            "manifest_hash": manifest.manifest_hash,
            "data_fingerprint": manifest.data_fingerprint,
            "evidence_bars": manifest.evidence.bar_count,
            "walkforward_bars": manifest.walkforward.bar_count,
            "lockbox_bars": manifest.lockbox.bar_count,
        }
    )
    return PreflightResult(
        verdict="ok",
        profile=profile,
        manifest=manifest,
        bars=bars,
        reasons=[],
        coverage=coverage,
    )
