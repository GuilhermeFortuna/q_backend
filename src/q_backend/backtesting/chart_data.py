from typing import Any, Dict, List, Optional

import pandas as pd

from q_backend.backtesting.signal_columns import BAR_INDEX, SIGNAL_COLUMNS
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso


def _to_iso_timestamp(ts: Any) -> str:
    if isinstance(ts, pd.Timestamp):
        dt = ts.to_pydatetime()
    else:
        dt = pd.Timestamp(ts).to_pydatetime()
    return mt5_datetime_to_utc_iso(dt)


def _series_values(column: pd.Series) -> List[Optional[float]]:
    values: List[Optional[float]] = []
    for val in column:
        if pd.isna(val):
            values.append(None)
        else:
            values.append(float(val))
    return values


_PLOT_STYLE_FIELDS = ("shape", "size", "position", "negative_color", "baseline", "fill_to", "fill_opacity")


def _segment_positions(df: pd.DataFrame, spec: ChartIndicatorSpec) -> List[Optional[int]]:
    """Bar position of each segment's start bar, or None where the label is missing from the frame."""
    position_by_label = {label: position for position, label in enumerate(df.index)}
    return [position_by_label.get(label) for label in df[spec.start_column]]


def serialize_chart_data(
    df: pd.DataFrame,
    strategy: TradingStrategy,
) -> Dict[str, Any]:
    """
    Serialize OHLCV bars and strategy indicator series from an augmented DataFrame.
    """
    specs = strategy.get_chart_indicators()

    bars: List[Dict[str, Any]] = []
    for ts, row in df.iterrows():
        volume = row.get("tick_volume", row.get("volume", 0))
        bars.append(
            {
                "timestamp": _to_iso_timestamp(ts),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(volume),
            }
        )

    indicators: List[Dict[str, Any]] = []
    for spec in specs:
        if spec.key not in df.columns:
            continue
        column = df[spec.key]
        entry: Dict[str, Any] = {
            "key": spec.key,
            "label": spec.label,
            "pane": spec.pane,
            "color": spec.color,
            "line_style": spec.line_style,
            "line_width": spec.line_width,
        }
        # Plot-kind fields are emitted only when they differ from a plain line, so existing line
        # series serialize exactly as before.
        if spec.kind != "line":
            entry["kind"] = spec.kind
        for field in _PLOT_STYLE_FIELDS:
            value = getattr(spec, field)
            if value is not None:
                entry[field] = value
        if spec.kind == "segment":
            start_index = _segment_positions(df, spec)
            # A segment without a resolvable start bar is dropped rather than drawn from the wrong bar.
            column = column.where(pd.Series([p is not None for p in start_index], index=column.index))
            entry["start_index"] = start_index
        entry["values"] = _series_values(column)
        indicators.append(entry)

    return {"bars": bars, "indicators": indicators}


def market_data_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flatten an augmented backtest DataFrame into an export table.

    Keeps the bars and every indicator column the strategy and its exit rules
    computed, drops the internal signal-contract columns, and moves the bar
    index into a leading ISO-8601 UTC ``time`` column.
    """
    internal = [column for column in df.columns if column in SIGNAL_COLUMNS or column == BAR_INDEX]
    frame = df.drop(columns=internal)
    frame.insert(0, "time", [_to_iso_timestamp(ts) for ts in frame.index])
    return frame.reset_index(drop=True)
