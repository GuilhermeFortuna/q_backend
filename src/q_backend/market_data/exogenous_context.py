"""Causal cross-instrument context attachment for research runs (WO160)."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.technical_indicators import compute_realized_vol
from q_backend.backtesting.transforms import compute_rolling_zscore
from q_backend.market_data.exogenous_config import ExogenousSeriesConfig
from q_backend.market_data.exogenous_columns import exog_column_name

LoadOhlcvFn = Callable[[str, str, datetime, datetime], pd.DataFrame]

_TF_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "H1": 60,
    "D1": 1440,
}

_PANDAS_RULE: dict[str, str] = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "D1": "1D",
}

# Fail preflight when source continuity breaks exceed this many calendar days.
_MAX_SOURCE_GAP_DAYS = 31

_EVAL_FRAME_CACHE: OrderedDict[str, tuple[pd.DataFrame, list[dict[str, Any]]]] = OrderedDict()
_EVAL_FRAME_CACHE_MAX = 8


def bar_duration(timeframe: str) -> pd.Timedelta:
    key = timeframe.upper()
    if key not in _TF_MINUTES:
        raise ValueError(f"Unsupported timeframe '{timeframe}'")
    return pd.Timedelta(minutes=_TF_MINUTES[key])


def _evaluation_cache_key(
    primary_symbol: str,
    primary_timeframe: str,
    start: datetime,
    end: datetime,
    exogenous_series: list[ExogenousSeriesConfig],
) -> str:
    payload = {
        "primary_symbol": primary_symbol,
        "primary_timeframe": primary_timeframe.upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "exogenous": [spec.model_dump(mode="json") for spec in exogenous_series],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _cache_put(key: str, frame: pd.DataFrame, provenance: list[dict[str, Any]]) -> None:
    _EVAL_FRAME_CACHE[key] = (frame, provenance)
    while len(_EVAL_FRAME_CACHE) > _EVAL_FRAME_CACHE_MAX:
        _EVAL_FRAME_CACHE.popitem(last=False)


def clear_evaluation_frame_cache() -> None:
    _EVAL_FRAME_CACHE.clear()


def get_cached_exogenous_provenance(
    *,
    primary_symbol: str,
    primary_timeframe: str,
    start: datetime,
    end: datetime,
    exogenous_series: list[ExogenousSeriesConfig],
) -> list[dict[str, Any]]:
    key = _evaluation_cache_key(primary_symbol, primary_timeframe, start, end, exogenous_series)
    cached = _EVAL_FRAME_CACHE.get(key)
    if cached is None:
        return []
    return list(cached[1])


def _bar_close_times(index: pd.DatetimeIndex, timeframe: str) -> pd.DatetimeIndex:
    return index + bar_duration(timeframe)


def resample_completed_bars(
    frame: pd.DataFrame,
    *,
    source_timeframe: str,
    target_timeframe: str,
) -> pd.DataFrame:
    """Aggregate finer bars into coarser ones using only completed source bars."""
    src = source_timeframe.upper()
    tgt = target_timeframe.upper()
    if src == tgt:
        return frame.sort_index()

    if bar_duration(src) >= bar_duration(tgt):
        raise ValueError(f"Cannot resample {source_timeframe} to finer timeframe {target_timeframe}")

    working = frame.sort_index().copy()
    close_times = _bar_close_times(working.index, src)
    working = working.assign(_close_time=close_times).set_index("_close_time")
    rule = _PANDAS_RULE[tgt]
    aggregated = (
        working.resample(rule, label="right", closed="right")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["close"])
    )
    return aggregated


def _preflight_source_continuity(frame: pd.DataFrame, *, symbol: str, timeframe: str) -> None:
    if frame.empty:
        raise ValueError(f"No exogenous data for {symbol} {timeframe}")
    gaps = frame.index.to_series().diff().dropna()
    if gaps.empty:
        return
    max_gap = gaps.max()
    if max_gap > pd.Timedelta(days=_MAX_SOURCE_GAP_DAYS):
        raise ValueError(
            f"Exogenous series {symbol} {timeframe} has a gap of {max_gap.days} days; "
            f"maximum allowed is {_MAX_SOURCE_GAP_DAYS} days"
        )


def _preflight_overlap(
    primary_index: pd.DatetimeIndex,
    source_index: pd.DatetimeIndex,
    aligned_close: pd.Series,
    *,
    source_timeframe: str,
    symbol: str,
) -> None:
    primary_start = primary_index.min()
    primary_end = primary_index.max()
    source_end = source_index.max() + bar_duration(source_timeframe)
    source_start = source_index.min()
    if source_end < primary_start:
        raise ValueError(f"Exogenous series {symbol} does not overlap the primary evaluation range")
    if source_start > primary_end:
        raise ValueError(f"Exogenous series {symbol} does not overlap the primary evaluation range")
    valid = aligned_close.dropna()
    if valid.empty:
        raise ValueError(f"Exogenous alignment for {symbol} produced no overlapping observations")


def align_exogenous_close(
    primary_index: pd.DatetimeIndex,
    exog_frame: pd.DataFrame,
    *,
    aligned_timeframe: str,
    primary_timeframe: str,
    availability_lag_bars: int,
) -> pd.Series:
    """Backward as-of join of exogenous close onto primary bar open times."""
    if availability_lag_bars < 0:
        raise ValueError("availability_lag_bars cannot be negative")

    exog = exog_frame.sort_index()
    avail_times = _bar_close_times(exog.index, aligned_timeframe) + availability_lag_bars * bar_duration(
        primary_timeframe
    )
    right = pd.DataFrame(
        {
            "avail_time": avail_times,
            "close": exog["close"].to_numpy(),
        }
    ).sort_values("avail_time")

    left = pd.DataFrame({"primary_time": primary_index.sort_values()})
    merged = pd.merge_asof(
        left,
        right,
        left_on="primary_time",
        right_on="avail_time",
        direction="backward",
    )
    aligned = pd.Series(
        merged["close"].to_numpy(),
        index=primary_index,
        name="exog_close",
    )
    return aligned


def _compute_recipe_columns(
    *,
    primary_close: pd.Series,
    aligned_exog_close: pd.Series,
    spec: ExogenousSeriesConfig,
) -> dict[str, pd.Series]:
    symbol = spec.symbol
    lookback = spec.lookback_bars
    columns: dict[str, pd.Series] = {}

    if "close" in spec.recipes:
        columns[exog_column_name(symbol, "close")] = aligned_exog_close

    exog_return = aligned_exog_close / aligned_exog_close.shift(lookback) - 1.0
    primary_return = primary_close / primary_close.shift(lookback) - 1.0

    if "return" in spec.recipes:
        columns[exog_column_name(symbol, f"return_{lookback}")] = exog_return

    if "return_zscore" in spec.recipes:
        columns[exog_column_name(symbol, f"return_zscore_{lookback}_{spec.vol_window}")] = compute_rolling_zscore(
            exog_return, spec.vol_window
        )

    if "rolling_corr" in spec.recipes:
        columns[exog_column_name(symbol, f"rolling_corr_{spec.corr_window}")] = primary_return.rolling(
            spec.corr_window
        ).corr(exog_return)

    if "relative_strength" in spec.recipes:
        columns[exog_column_name(symbol, f"relative_strength_{lookback}")] = primary_return - exog_return

    if "vol_regime" in spec.recipes:
        exog_vol = compute_realized_vol(aligned_exog_close, spec.vol_window)
        rolling_pct = exog_vol.rolling(spec.vol_percentile_window).apply(
            lambda window: pd.Series(window).rank(pct=True).iloc[-1],
            raw=False,
        )
        columns[exog_column_name(symbol, f"vol_regime_{spec.vol_window}")] = (
            rolling_pct >= spec.vol_regime_threshold
        ).astype(float)

    if "direction_regime" in spec.recipes:
        columns[exog_column_name(symbol, f"direction_regime_{lookback}")] = (exog_return > 0).astype(float)

    return columns


def _content_fingerprint(frame: pd.DataFrame, columns: list[str]) -> str:
    payload: dict[str, Any] = {}
    for col in sorted(columns):
        series = frame[col].dropna()
        if series.empty:
            payload[col] = {"count": 0}
            continue
        payload[col] = {
            "count": int(series.count()),
            "first": float(series.iloc[0]),
            "last": float(series.iloc[-1]),
            "mean": float(series.mean()),
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_exogenous_context(
    primary_frame: pd.DataFrame,
    exogenous_series: list[ExogenousSeriesConfig],
    *,
    primary_symbol: str,
    primary_timeframe: str,
    loader: LoadOhlcvFn,
    start: datetime,
    end: datetime,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if not exogenous_series:
        return primary_frame, []

    frame = primary_frame.sort_index().copy()
    provenance: list[dict[str, Any]] = []
    primary_close = frame["close"]

    for spec in exogenous_series:
        source = loader(spec.symbol, spec.source_timeframe, start, end)
        _preflight_source_continuity(source, symbol=spec.symbol, timeframe=spec.source_timeframe)

        aligned_tf = spec.source_timeframe
        working = source
        if spec.resampling_rule == "last_completed":
            assert spec.target_timeframe is not None
            working = resample_completed_bars(
                source,
                source_timeframe=spec.source_timeframe,
                target_timeframe=spec.target_timeframe,
            )
            aligned_tf = spec.target_timeframe

        aligned_close = align_exogenous_close(
            frame.index,
            working,
            aligned_timeframe=aligned_tf,
            primary_timeframe=primary_timeframe,
            availability_lag_bars=spec.availability_lag_bars,
        )
        _preflight_overlap(
            frame.index,
            source.index,
            aligned_close,
            source_timeframe=spec.source_timeframe,
            symbol=spec.symbol,
        )

        recipe_columns = _compute_recipe_columns(
            primary_close=primary_close,
            aligned_exog_close=aligned_close,
            spec=spec,
        )
        for col, series in recipe_columns.items():
            frame[col] = series

        provenance.append(
            {
                "symbol": spec.symbol,
                "source_timeframe": spec.source_timeframe.upper(),
                "aligned_timeframe": aligned_tf.upper(),
                "resampling_rule": spec.resampling_rule,
                "target_timeframe": (spec.target_timeframe.upper() if spec.target_timeframe else None),
                "availability_lag_bars": spec.availability_lag_bars,
                "recipes": list(spec.recipes),
                "source_range": {
                    "start": source.index.min().isoformat(),
                    "end": source.index.max().isoformat(),
                },
                "columns": sorted(recipe_columns.keys()),
                "content_fingerprint": _content_fingerprint(frame, sorted(recipe_columns.keys())),
            }
        )

    return frame, provenance


def prepare_evaluation_frame(
    *,
    primary_symbol: str,
    primary_timeframe: str,
    start: datetime,
    end: datetime,
    exogenous_series: list[ExogenousSeriesConfig],
    loader: LoadOhlcvFn,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    key = _evaluation_cache_key(primary_symbol, primary_timeframe, start, end, exogenous_series)
    cached = _EVAL_FRAME_CACHE.get(key)
    if cached is not None:
        return cached[0].copy(), list(cached[1])

    primary = loader(primary_symbol, primary_timeframe, start, end)
    if not exogenous_series:
        _cache_put(key, primary, [])
        return primary.copy(), []

    frame, provenance = attach_exogenous_context(
        primary,
        exogenous_series,
        primary_symbol=primary_symbol,
        primary_timeframe=primary_timeframe,
        loader=loader,
        start=start,
        end=end,
    )
    _cache_put(key, frame, provenance)
    return frame.copy(), provenance
