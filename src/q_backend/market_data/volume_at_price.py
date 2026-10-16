"""Volume-at-price profiles and value-area levels from the research tick store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from q_backend.backtesting.technical_indicators import compute_volume_at_price, compute_volume_at_price_levels
from q_backend.market_data.clients.shared import _time_msc_to_naive_local
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.quote_spread import _session_key, bar_bounds_msc
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.frame import parse_query_bound
from q_backend.research.tick_store import TickStore, naive_local_to_time_msc

ByMode = Literal["session", "bar", "range"]
LevelsMode = Literal["developing", "prior_session"]

_ROW_TICKS_MIN = 1
_ROW_TICKS_MAX = 1_000
_VALUE_AREA_MIN = 0.05
_VALUE_AREA_MAX = 1.0


@dataclass(frozen=True)
class VolumeProfile:
    """Frozen volume-at-price result with segment-indexed rows and summaries."""

    rows: pd.DataFrame
    segments: pd.DataFrame
    unavailable_reason: str | None = None


def _validate_tick_size(tick_size: float) -> float:
    if not isinstance(tick_size, (int, float, np.floating, np.integer)):
        raise TypeError("'tick_size' must be a number")
    value = float(tick_size)
    if not np.isfinite(value) or value <= 0:
        raise ValueError("'tick_size' must be a finite number > 0")
    return value


def _validate_row_ticks(row_ticks: int) -> int:
    if isinstance(row_ticks, bool) or not isinstance(row_ticks, (int, np.integer)):
        raise TypeError("'row_ticks' must be an integer")
    parsed = int(row_ticks)
    if parsed < _ROW_TICKS_MIN or parsed > _ROW_TICKS_MAX:
        raise ValueError(f"'row_ticks' must be between {_ROW_TICKS_MIN} and {_ROW_TICKS_MAX} (got {parsed}).")
    return parsed


def _validate_value_area(value_area: float) -> float:
    if isinstance(value_area, bool) or not isinstance(value_area, (int, float, np.floating, np.integer)):
        raise TypeError("'value_area' must be a number")
    parsed = float(value_area)
    if not np.isfinite(parsed) or parsed < _VALUE_AREA_MIN or parsed > _VALUE_AREA_MAX:
        raise ValueError(
            f"'value_area' must be finite and between {_VALUE_AREA_MIN} and {_VALUE_AREA_MAX} (got {parsed})."
        )
    return parsed


def _validate_by(by: str) -> ByMode:
    if not isinstance(by, str):
        raise TypeError("'by' must be a string")
    mode = by.strip().lower()
    if mode not in ("session", "bar", "range"):
        raise ValueError("'by' must be session, bar, or range")
    return mode  # type: ignore[return-value]


def _validate_levels_mode(mode: str) -> LevelsMode:
    if not isinstance(mode, str):
        raise TypeError("'mode' must be a string")
    parsed = mode.strip().lower()
    if parsed not in ("developing", "prior_session"):
        raise ValueError("'mode' must be developing or prior_session")
    return parsed  # type: ignore[return-value]


def _ts_from_msc(value: int) -> pd.Timestamp:
    naive = _time_msc_to_naive_local(int(value))
    return pd.Timestamp(naive).tz_localize(BRASILIA_TZ)


def _day_start_msc(day: date) -> int:
    return naive_local_to_time_msc(datetime(day.year, day.month, day.day))


def _day_end_msc(day: date) -> int:
    return _day_start_msc(day + timedelta(days=1))


def _query_end_exclusive_msc(end_ts: pd.Timestamp) -> int:
    end_naive = end_ts.tz_convert(BRASILIA_TZ).tz_localize(None).to_pydatetime()
    return naive_local_to_time_msc(end_naive)


def _empty_profile(reason: str | None) -> VolumeProfile:
    rows = pd.DataFrame(
        columns=["price", "buy", "sell", "unknown", "total", "delta", "trades"],
        index=pd.DatetimeIndex([], tz=BRASILIA_TZ, name="segment_start"),
    )
    segments = pd.DataFrame(
        columns=[
            "end",
            "total",
            "buy",
            "sell",
            "unknown",
            "trades",
            "poc",
            "vah",
            "val",
            "skipped_trades",
            "off_grid_trades",
        ],
        index=pd.DatetimeIndex([], tz=BRASILIA_TZ, name="segment_start"),
    )
    return VolumeProfile(rows, segments, reason)


def _load_trades(
    store: TickStore,
    start_ts: pd.Timestamp,
    end_exclusive_msc: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[date]]:
    start_msc = _query_end_exclusive_msc(start_ts)
    end_inclusive = _ts_from_msc(end_exclusive_msc - 1) if end_exclusive_msc > start_msc else start_ts
    days = store._session_days_in_range(start_ts, end_inclusive)
    if not days:
        return (
            np.array([], dtype=np.int64),
            np.array([], dtype=np.float64),
            np.array([], dtype=np.float64),
            np.array([], dtype=np.int64),
            np.array([], dtype=np.int64),
            [],
        )

    chunks_t: list[np.ndarray] = []
    chunks_p: list[np.ndarray] = []
    chunks_v: list[np.ndarray] = []
    chunks_f: list[np.ndarray] = []
    chunks_s: list[np.ndarray] = []
    for day in days:
        time_msc, last, volume, flags = store.trades(day)
        session_key = _session_key(day)
        mask = (time_msc >= start_msc) & (time_msc < end_exclusive_msc)
        if not mask.any():
            continue
        chunks_t.append(time_msc[mask])
        chunks_p.append(last[mask])
        chunks_v.append(volume[mask])
        chunks_f.append(flags[mask])
        chunks_s.append(np.full(mask.sum(), session_key, dtype=np.int64))

    if not chunks_t:
        return (
            np.array([], dtype=np.int64),
            np.array([], dtype=np.float64),
            np.array([], dtype=np.float64),
            np.array([], dtype=np.int64),
            np.array([], dtype=np.int64),
            days,
        )

    return (
        np.concatenate(chunks_t),
        np.concatenate(chunks_p),
        np.concatenate(chunks_v),
        np.concatenate(chunks_f),
        np.concatenate(chunks_s),
        days,
    )


def _build_segments(
    *,
    by: ByMode,
    start_ts: pd.Timestamp,
    end_exclusive_msc: int,
    days: list[date],
    bar_times: pd.DatetimeIndex | None,
    timeframe: str | None,
) -> tuple[np.ndarray, np.ndarray, list[pd.Timestamp], list[pd.Timestamp]]:
    opens: list[int] = []
    closes: list[int] = []
    start_labels: list[pd.Timestamp] = []
    end_labels: list[pd.Timestamp] = []
    start_msc = _query_end_exclusive_msc(start_ts)

    if by == "range":
        if end_exclusive_msc <= start_msc:
            raise ValueError("start must be < end for range segments")
        opens.append(start_msc)
        closes.append(end_exclusive_msc)
        start_labels.append(_ts_from_msc(start_msc))
        end_labels.append(_ts_from_msc(end_exclusive_msc))
        return (
            np.asarray(opens, dtype=np.int64),
            np.asarray(closes, dtype=np.int64),
            start_labels,
            end_labels,
        )

    if by == "session":
        for day in days:
            day_open = max(start_msc, _day_start_msc(day))
            day_close = min(end_exclusive_msc, _day_end_msc(day))
            if day_close <= day_open:
                continue
            opens.append(day_open)
            closes.append(day_close)
            start_labels.append(_ts_from_msc(day_open))
            end_labels.append(_ts_from_msc(day_close))
        return (
            np.asarray(opens, dtype=np.int64),
            np.asarray(closes, dtype=np.int64),
            start_labels,
            end_labels,
        )

    if bar_times is None or timeframe is None:
        raise ValueError("'timeframe' is required when by='bar'")
    tf = timeframe.strip().upper()
    bar_duration(tf)
    bar_open, bar_close = bar_bounds_msc(bar_times, tf)
    for open_msc, close_msc in zip(bar_open, bar_close, strict=True):
        open_i = int(open_msc)
        close_i = int(close_msc)
        if close_i <= open_i:
            continue
        if close_i <= start_msc or open_i >= end_exclusive_msc:
            continue
        seg_open = max(open_i, start_msc)
        seg_close = min(close_i, end_exclusive_msc)
        if seg_close <= seg_open:
            continue
        opens.append(seg_open)
        closes.append(seg_close)
        start_labels.append(_ts_from_msc(seg_open))
        end_labels.append(_ts_from_msc(seg_close))
    return (
        np.asarray(opens, dtype=np.int64),
        np.asarray(closes, dtype=np.int64),
        start_labels,
        end_labels,
    )


def _frames_from_kernel(
    rows_out: dict,
    segments_out: dict,
    start_labels: list[pd.Timestamp],
    end_labels: list[pd.Timestamp],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    seg_index = pd.DatetimeIndex(start_labels, tz=BRASILIA_TZ, name="segment_start")
    segment_ids = np.asarray(rows_out["segment"], dtype=np.int64)
    row_index = seg_index.take(segment_ids)
    rows = pd.DataFrame(
        {
            "price": np.asarray(rows_out["price"], dtype=np.float64),
            "buy": np.asarray(rows_out["buy"], dtype=np.float64),
            "sell": np.asarray(rows_out["sell"], dtype=np.float64),
            "unknown": np.asarray(rows_out["unknown"], dtype=np.float64),
            "total": np.asarray(rows_out["total"], dtype=np.float64),
            "delta": np.asarray(rows_out["delta"], dtype=np.float64),
            "trades": np.asarray(rows_out["trades"], dtype=np.int64),
        },
        index=row_index,
    )
    segments = pd.DataFrame(
        {
            "end": pd.DatetimeIndex(end_labels, tz=BRASILIA_TZ),
            "total": np.asarray(segments_out["total"], dtype=np.float64),
            "buy": np.asarray(segments_out["buy"], dtype=np.float64),
            "sell": np.asarray(segments_out["sell"], dtype=np.float64),
            "unknown": np.asarray(segments_out["unknown"], dtype=np.float64),
            "trades": np.asarray(segments_out["trades"], dtype=np.int64),
            "poc": np.asarray(segments_out["poc"], dtype=np.float64),
            "vah": np.asarray(segments_out["vah"], dtype=np.float64),
            "val": np.asarray(segments_out["val"], dtype=np.float64),
            "skipped_trades": np.asarray(segments_out["skipped_trades"], dtype=np.int64),
            "off_grid_trades": np.asarray(segments_out["off_grid_trades"], dtype=np.int64),
        },
        index=seg_index,
    )
    return rows, segments


def build_volume_profile(
    store: TickStore | None,
    symbol: str,
    *,
    start: str | datetime,
    end: str | datetime,
    tick_size: float,
    by: ByMode = "session",
    timeframe: str | None = None,
    row_ticks: int = 1,
    value_area: float = 0.7,
    bar_times: pd.DatetimeIndex | None = None,
) -> VolumeProfile:
    """Shared volume-at-price builder for research and the Market profile route."""
    tick = _validate_tick_size(tick_size)
    rows_per = _validate_row_ticks(row_ticks)
    area = _validate_value_area(value_area)
    mode = _validate_by(by)

    start_ts = parse_query_bound(start)
    end_ts = parse_query_bound(end)
    if start_ts > end_ts:
        raise ValueError("start must be <= end")
    start_msc = _query_end_exclusive_msc(start_ts)
    end_exclusive_msc = _query_end_exclusive_msc(end_ts)
    if end_exclusive_msc <= start_msc:
        raise ValueError("start must be < end")

    if store is None:
        return _empty_profile(f"The tick store is not available for {symbol}.")

    time_msc, price, volume, flags, session, days = _load_trades(store, start_ts, end_exclusive_msc)
    if time_msc.size == 0:
        first = start_ts.tz_convert(BRASILIA_TZ).date().isoformat()
        final = end_ts.tz_convert(BRASILIA_TZ).date().isoformat()
        return _empty_profile(f"No ticks are stored for {symbol} between {first} and {final}.")

    try:
        seg_open, seg_close, start_labels, end_labels = _build_segments(
            by=mode,
            start_ts=start_ts,
            end_exclusive_msc=end_exclusive_msc,
            days=days,
            bar_times=bar_times,
            timeframe=timeframe,
        )
    except ValueError:
        raise
    if seg_open.size == 0:
        return _empty_profile(None)

    rows_out, segments_out = compute_volume_at_price(
        time_msc,
        price,
        volume,
        flags,
        seg_open,
        seg_close,
        tick,
        rows_per,
        area,
    )
    rows, segments = _frames_from_kernel(rows_out, segments_out, start_labels, end_labels)
    return VolumeProfile(rows, segments, None)


def value_area_levels_from_ticks(
    frame: pd.DataFrame,
    store: TickStore | None,
    symbol: str,
    *,
    timeframe: str,
    mode: LevelsMode,
    tick_size: float,
    row_ticks: int = 1,
    value_area: float = 0.7,
) -> tuple[pd.DataFrame, str | None]:
    """POC, VAH and VAL aligned to ``frame``'s index."""
    area = _validate_value_area(value_area)
    rows_per = _validate_row_ticks(row_ticks)
    tick = _validate_tick_size(tick_size)
    levels_mode = _validate_levels_mode(mode)
    tf = timeframe.strip().upper()
    bar_duration(tf)

    if store is None:
        return (
            pd.DataFrame({"poc": np.nan, "vah": np.nan, "val": np.nan}, index=frame.index),
            f"The tick store is not available for {symbol}.",
        )

    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.empty:
        empty = pd.DataFrame({"poc": [], "vah": [], "val": []}, index=frame.index)
        return empty, None

    start_ts = frame.index[0]
    end_ts = frame.index[-1]
    end_exclusive_msc = _query_end_exclusive_msc(end_ts) + int(bar_duration(tf) / pd.Timedelta(milliseconds=1))
    time_msc, price, volume, flags, session, days = _load_trades(store, start_ts, end_exclusive_msc)
    if time_msc.size == 0:
        first = start_ts.tz_convert(BRASILIA_TZ).date().isoformat()
        final = end_ts.tz_convert(BRASILIA_TZ).date().isoformat()
        nan_frame = pd.DataFrame(
            {"poc": np.nan, "vah": np.nan, "val": np.nan},
            index=frame.index,
        )
        return nan_frame, f"No ticks are stored for {symbol} between {first} and {final}."

    bar_open, bar_close = bar_bounds_msc(frame.index, tf)
    checkpoint_msc = bar_close.astype(np.int64)
    bar_days = frame.index.tz_convert(BRASILIA_TZ).date if frame.index.tz else frame.index.date
    checkpoint_session = np.array([_session_key(day) for day in bar_days], dtype=np.int64)

    if levels_mode == "prior_session":
        session_profiles: dict[int, tuple[float, float, float]] = {}
        for day in sorted(set(days)):
            profile = build_volume_profile(
                store,
                symbol,
                start=day.isoformat(),
                end=(day + timedelta(days=1)).isoformat(),
                tick_size=tick,
                by="session",
                row_ticks=rows_per,
                value_area=area,
            )
            if profile.segments.empty:
                continue
            row = profile.segments.iloc[0]
            session_profiles[_session_key(day)] = (row["poc"], row["vah"], row["val"])

        poc = np.full(len(frame), np.nan)
        vah = np.full(len(frame), np.nan)
        val = np.full(len(frame), np.nan)
        ordered_days = sorted(session_profiles)
        for index, day in enumerate(bar_days):
            prior = None
            for stored_day in reversed(ordered_days):
                if stored_day < _session_key(day):
                    prior = session_profiles[stored_day]
                    break
            if prior is not None:
                poc[index], vah[index], val[index] = prior
        out = pd.DataFrame({"poc": poc, "vah": vah, "val": val}, index=frame.index)
        return out, None

    levels = compute_volume_at_price_levels(
        time_msc,
        price,
        volume,
        flags,
        session,
        checkpoint_msc,
        checkpoint_session,
        tick,
        rows_per,
        area,
    )
    out = pd.DataFrame(
        {
            "poc": np.asarray(levels["poc"], dtype=np.float64),
            "vah": np.asarray(levels["vah"], dtype=np.float64),
            "val": np.asarray(levels["val"], dtype=np.float64),
        },
        index=frame.index,
    )
    return out, None


def profile_to_api_payload(
    profile: VolumeProfile,
    *,
    tick_size: float,
    row_ticks: int,
) -> dict:
    """Serialize a profile for the Market volume-profile route."""
    row_size = float(tick_size) * int(row_ticks)
    if profile.unavailable_reason is not None:
        return {
            "tickSize": tick_size,
            "rowSize": row_size,
            "segments": [],
            "unavailableReason": profile.unavailable_reason,
        }
    segments_payload: list[dict] = []
    if profile.segments.empty:
        return {"tickSize": tick_size, "rowSize": row_size, "segments": [], "unavailableReason": None}

    rows = profile.rows
    for start, summary in profile.segments.iterrows():
        mask = rows.index == start
        part = rows.loc[mask]
        segments_payload.append(
            {
                "start": start.isoformat(),
                "end": summary["end"].isoformat(),
                "total": float(summary["total"]),
                "buy": float(summary["buy"]),
                "sell": float(summary["sell"]),
                "unknown": float(summary["unknown"]),
                "trades": int(summary["trades"]),
                "poc": float(summary["poc"]),
                "vah": float(summary["vah"]),
                "val": float(summary["val"]),
                "rows": {
                    "price": part["price"].tolist(),
                    "buy": part["buy"].tolist(),
                    "sell": part["sell"].tolist(),
                    "unknown": part["unknown"].tolist(),
                },
            }
        )
    return {
        "tickSize": tick_size,
        "rowSize": row_size,
        "segments": segments_payload,
        "unavailableReason": None,
    }
