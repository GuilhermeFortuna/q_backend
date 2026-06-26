"""Feature matrix builder with content-addressed lake cache (WO129).

Invalidation: ``matrix_id`` is a SHA-256 of ``symbol``, ``timeframe``, ``start``,
``end``, ``engine_version``, and each requested feature's ``(feature_id, version,
params)``. Bumping ``ENGINE_VERSION`` or a feature's catalog ``version`` yields a
new id — old matrices are never silently served for new semantics. There is no TTL;
artifacts are immutable; a refresh is a new id, not an in-place mutation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from q_backend.features.compute import compute_feature
from q_backend.features.registry import feature_id, get_feature_spec, resolve_params
from q_backend.market_data.local_store import read_ohlcv
from q_backend.market_data.models import OHLCV
from q_backend.storage.lake.artifacts import (
    feature_matrix_exists,
    read_feature_matrix,
    write_feature_matrix,
)

ENGINE_VERSION = 1


@dataclass(frozen=True)
class FeatureRequest:
    name: str
    version: int | None
    params: dict[str, Any]


@dataclass(frozen=True)
class FeatureMatrix:
    matrix_id: str
    frame: pd.DataFrame
    manifest: dict[str, Any]


def _iso_z(dt: datetime) -> str:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ohlcv_to_compute_bars(bars: list[OHLCV]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    records = []
    for bar in bars:
        records.append(
            {
                "time": pd.Timestamp(bar.time),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.tick_volume),
            }
        )
    return pd.DataFrame(records)


def _resolve_request(
    request: FeatureRequest,
) -> tuple[Any, dict[str, Any], str]:
    spec = get_feature_spec(request.name, version=request.version)
    params = resolve_params(spec, request.params)
    fid = feature_id(spec, params)
    return spec, params, fid


def _matrix_cache_payload(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    feature_entries: list[tuple[str, int, dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "start": _iso_z(start),
        "end": _iso_z(end),
        "engine_version": ENGINE_VERSION,
        "features": sorted(
            [
                {
                    "feature_id": fid,
                    "version": version,
                    "params": dict(sorted(params.items())),
                }
                for fid, version, params in feature_entries
            ],
            key=lambda item: item["feature_id"],
        ),
    }


def compute_matrix_id(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    features: list[FeatureRequest],
) -> str:
    entries: list[tuple[str, int, dict[str, Any]]] = []
    for request in features:
        spec, params, fid = _resolve_request(request)
        entries.append((fid, spec.version, params))
    payload = _matrix_cache_payload(symbol, timeframe, start, end, entries)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _union_valid_from(times: pd.Series, union_warmup: int) -> str | None:
    if union_warmup >= len(times):
        return None
    return _iso_z(pd.Timestamp(times.iloc[union_warmup]).to_pydatetime())


def _build_manifest(
    matrix_id: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    times: pd.Series,
    feature_manifest_rows: list[dict[str, Any]],
    union_warmup: int,
) -> dict[str, Any]:
    return {
        "matrix_id": matrix_id,
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "start": _iso_z(start),
        "end": _iso_z(end),
        "bar_count": int(len(times)),
        "valid_from": _union_valid_from(times, union_warmup),
        "features": feature_manifest_rows,
        "engine_version": ENGINE_VERSION,
        "built_at": _iso_z(datetime.now(timezone.utc)),
    }


def build_feature_matrix(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    features: list[FeatureRequest],
    *,
    use_cache: bool = True,
) -> FeatureMatrix:
    if not features:
        raise ValueError("features must contain at least one FeatureRequest")

    matrix_id = compute_matrix_id(symbol, timeframe, start, end, features)
    if use_cache and feature_matrix_exists(matrix_id):
        cached = read_feature_matrix(matrix_id)
        return FeatureMatrix(
            matrix_id=matrix_id,
            frame=cached.frame,
            manifest=cached.manifest,
        )

    bars = read_ohlcv(symbol, timeframe, start, end)
    bars_df = _ohlcv_to_compute_bars(bars)

    columns: dict[str, pd.Series] = {}
    feature_manifest_rows: list[dict[str, Any]] = []
    union_warmup = 0

    for request in features:
        spec, params, fid = _resolve_request(request)
        computed = compute_feature(bars_df, spec, request.params)
        columns[fid] = computed.series
        union_warmup = max(union_warmup, computed.warmup_bars)
        feature_manifest_rows.append(
            {
                "feature_id": fid,
                "name": request.name,
                "version": spec.version,
                "params": dict(sorted(params.items())),
                "warmup_bars": computed.warmup_bars,
                "leakage_status": computed.leakage_status,
            }
        )

    if bars_df.empty:
        frame = pd.DataFrame(columns=sorted(columns))
    else:
        frame = pd.DataFrame(columns)
        frame.index = bars_df["time"]
        frame = frame.sort_index()
        frame = frame[sorted(frame.columns)]

    times = bars_df["time"] if not bars_df.empty else pd.Series(dtype="datetime64[ns, UTC]")
    manifest = _build_manifest(
        matrix_id,
        symbol,
        timeframe,
        start,
        end,
        times,
        sorted(feature_manifest_rows, key=lambda row: row["feature_id"]),
        union_warmup,
    )
    write_feature_matrix(matrix_id, frame, manifest)
    return FeatureMatrix(matrix_id=matrix_id, frame=frame, manifest=manifest)
