"""Client publishing for time-indexed studies drawn on the Market chart."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional, Union

import numpy as np
import pandas as pd

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.charting import CHART_PANES, ChartIndicator
from q_backend.research.provenance import collect_figure_provenance
from q_backend.research.transport import post_research_json

# Chart timeframes and their length in minutes, as the Market chart draws them.
CHART_TIMEFRAMES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M10": 10,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H2": 120,
    "H4": 240,
    "D1": 1440,
}


def publish_study(
    data: Union[pd.Series, pd.DataFrame],
    *,
    symbol: str,
    name: str,
    timeframe: Optional[str] = None,
    series: Optional[list[ChartIndicator]] = None,
    pane: str = "price",
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a time-indexed Series or DataFrame as a study for one symbol and timeframe.

    The Market chart lists the study for ``symbol`` and draws it on the bars with the same
    timestamps. Returns the study id.
    """
    # 1. Shape and index
    if isinstance(data, pd.Series):
        frame = data.to_frame(name=data.name if data.name is not None else "value")
    elif isinstance(data, pd.DataFrame):
        frame = data.copy()
    else:
        raise TypeError(f"publish_study expects a pandas Series or DataFrame, got {type(data)!r}")
    if len(frame) == 0:
        raise ValueError("Study data has no rows.")
    frame.columns = [str(column) for column in frame.columns]
    if frame.columns.duplicated().any():
        raise ValueError(
            f"Study data has duplicate column names: {sorted(set(frame.columns[frame.columns.duplicated()]))}"
        )

    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"Study data needs a DatetimeIndex, got {type(index).__name__}")
    if index.hasnans:
        raise ValueError("Study data index contains missing timestamps.")
    if not (index.is_monotonic_increasing and index.is_unique):
        raise ValueError("Study data index must be strictly increasing.")

    # 2. Timestamps in UTC; a naive index is Brasília exchange time
    if index.tz is None:
        utc_index = index.tz_localize(BRASILIA_TZ).tz_convert("UTC")
    else:
        utc_index = index.tz_convert("UTC")

    # 3. Timeframe
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Study symbol is required.")
    timeframe = _resolve_timeframe(utc_index, timeframe)

    # 4. Series selection and values
    if pane not in CHART_PANES:
        raise ValueError(f"Study pane must be one of {CHART_PANES!r}, got {pane!r}")
    if series is None:
        selected = [ChartIndicator(column, pane=pane) for column in frame.columns]
    else:
        if not series:
            raise ValueError("series= must name at least one column.")
        selected = list(series)
    keys = [indicator.column for indicator in selected]
    if len(set(keys)) != len(keys):
        raise ValueError(f"series= names a column more than once: {sorted({k for k in keys if keys.count(k) > 1})}")
    missing = [key for key in keys if key not in frame.columns]
    if missing:
        raise ValueError(f"series= names columns missing from the data: {missing}")
    for indicator in selected:
        if indicator.kind != "segment":
            continue
        start_column = indicator.start_column
        if not start_column:
            raise ValueError(f"Series {indicator.column!r} with kind 'segment' requires start_column")
        if start_column not in frame.columns:
            raise ValueError(f"Series {indicator.column!r} start_column {start_column!r} is missing from the data")
    selected_by_key = {indicator.column: indicator for indicator in selected}
    for indicator in selected:
        if indicator.kind == "segment" and indicator.fill_to is not None:
            raise ValueError(f"Series {indicator.column!r} with kind 'segment' cannot use fill_to")
        if indicator.fill_to is None:
            continue
        target = selected_by_key.get(indicator.fill_to)
        if indicator.fill_to == indicator.column:
            raise ValueError(f"Series {indicator.column!r} cannot fill to itself.")
        if target is None:
            raise ValueError(f"fill_to target {indicator.fill_to!r} is not selected.")
        if target.pane != indicator.pane:
            raise ValueError(f"fill_to target {indicator.fill_to!r} must use the same pane.")
        if target.kind not in {"line", "step"}:
            raise ValueError(f"fill_to target {indicator.fill_to!r} must be kind 'line' or 'step'.")

    wire_series: list[dict[str, Any]] = []
    for indicator in selected:
        column = frame[indicator.column]
        values = _column_values(column, indicator.column)
        if indicator.kind == "segment":
            assert indicator.start_column is not None
            start_index = _segment_positions(frame, indicator.start_column)
            values = [
                value if position is not None else None for value, position in zip(values, start_index, strict=True)
            ]
        item: dict[str, Any] = {
            "key": indicator.column,
            "label": indicator.label,
            "pane": indicator.pane,
            "values": values,
        }
        if indicator.kind == "segment":
            item["startIndex"] = start_index
        if indicator.color is not None:
            item["color"] = indicator.color
        if indicator.line_style is not None:
            item["lineStyle"] = indicator.line_style
        if indicator.line_width is not None:
            item["lineWidth"] = indicator.line_width
        if indicator.kind != "line":
            item["kind"] = indicator.kind
        for field, wire_name in (
            ("shape", "shape"),
            ("size", "size"),
            ("position", "position"),
            ("negative_color", "negativeColor"),
            ("baseline", "baseline"),
            ("fill_to", "fillTo"),
            ("fill_opacity", "fillOpacity"),
        ):
            value = getattr(indicator, field)
            if value is not None:
                item[wire_name] = value
        wire_series.append(item)

    # 5. Payload and HTTP dispatch
    payload = {
        "name": name,
        "symbol": symbol,
        "timeframe": timeframe,
        "notes": notes,
        "replace": replace,
        "provenance": collect_figure_provenance(),
        "timestamps": [timestamp.isoformat() for timestamp in utc_index],
        "series": wire_series,
    }
    res_data = post_research_json(
        "/api/v1/research/studies",
        payload,
        noun="study",
        endpoint="studies",
        api_url=api_url,
        client=client,
        conflict_error=not replace,
    )
    try:
        return str(res_data["studyId"])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected response format from studies endpoint: {res_data!r}") from exc


def _resolve_timeframe(utc_index: pd.DatetimeIndex, timeframe: Optional[str]) -> str:
    if timeframe is not None:
        normalized = timeframe.strip().upper()
        if normalized not in CHART_TIMEFRAMES:
            raise ValueError(f"timeframe= must be one of {list(CHART_TIMEFRAMES)}, got {timeframe!r}")
        step = pd.Timedelta(minutes=CHART_TIMEFRAMES[normalized])
    else:
        if len(utc_index) < 2:
            raise ValueError("Cannot infer the timeframe from fewer than two timestamps; pass timeframe=.")
        gaps = utc_index.to_series().diff().dropna()
        smallest = gaps.min()
        matches = [tf for tf, minutes in CHART_TIMEFRAMES.items() if smallest == pd.Timedelta(minutes=minutes)]
        if not matches:
            raise ValueError(
                f"Smallest gap between timestamps is {smallest}, which is not a chart timeframe; "
                "pass timeframe= for the bars the study was computed on."
            )
        normalized = matches[0]
        step = smallest

    if (utc_index.second != 0).any() or (utc_index.microsecond != 0).any() or (utc_index.nanosecond != 0).any():
        raise ValueError(
            f"Timestamps must fall on whole minutes for timeframe={normalized}; "
            "pass timeframe= for the bars the study was computed on."
        )
    if len(utc_index) >= 2:
        gaps = utc_index.to_series().diff().dropna()
        off_grid = gaps[(gaps % step) != pd.Timedelta(0)]
        if not off_grid.empty:
            raise ValueError(
                f"Gap of {off_grid.iloc[0]} between timestamps is not a whole multiple of "
                f"timeframe={normalized}; pass timeframe= for the bars the study was computed on."
            )
    return normalized


def slice_segment_start_index_for_window(
    full_start_index: list[Optional[int]],
    study_start: datetime,
    window_times: pd.DatetimeIndex,
    timeframe: str,
) -> list[Optional[int]]:
    """Rebase full-study segment starts onto a timestamp window returned by the series API."""
    if len(window_times) == 0:
        return []
    if timeframe not in CHART_TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {list(CHART_TIMEFRAMES)}, got {timeframe!r}")
    step = pd.Timedelta(minutes=CHART_TIMEFRAMES[timeframe])
    origin = pd.Timestamp(study_start)
    if origin.tzinfo is None:
        origin = origin.tz_localize("UTC")
    else:
        origin = origin.tz_convert("UTC")
    first = pd.Timestamp(window_times[0])
    if first.tzinfo is None:
        first = first.tz_localize("UTC")
    else:
        first = first.tz_convert("UTC")
    offset = int((first - origin) // step)
    sliced: list[Optional[int]] = []
    for i in range(len(window_times)):
        global_i = offset + i
        if global_i < 0 or global_i >= len(full_start_index):
            sliced.append(None)
            continue
        raw = full_start_index[global_i]
        if raw is None:
            sliced.append(None)
            continue
        rebased = raw - offset
        if rebased > i:
            sliced.append(None)
        elif rebased < 0:
            sliced.append(0)
        else:
            sliced.append(rebased)
    return sliced


def _segment_positions(frame: pd.DataFrame, start_column: str) -> list[Optional[int]]:
    """Bar position of each segment's start bar, or None where the label is missing from the frame."""
    position_by_label = {label: position for position, label in enumerate(frame.index)}
    positions: list[Optional[int]] = []
    for label in frame[start_column]:
        if label is None or (not isinstance(label, str) and pd.isna(label)):
            positions.append(None)
        else:
            positions.append(position_by_label.get(label))
    return positions


def _column_values(column: pd.Series, key: str) -> list[Optional[float]]:
    is_real = pd.api.types.is_bool_dtype(column) or (
        pd.api.types.is_numeric_dtype(column) and not pd.api.types.is_complex_dtype(column)
    )
    if not is_real:
        raise ValueError(f"Study column {key!r} must be numeric or boolean, got dtype {column.dtype}")
    values = column.to_numpy(dtype="float64", na_value=np.nan)
    values = np.where(np.isfinite(values), values, np.nan)
    return [None if math.isnan(value) else float(value) for value in values]
