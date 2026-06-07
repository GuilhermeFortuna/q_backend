from typing import Any, Dict, List, Optional

import pandas as pd

from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy


def _to_iso_timestamp(ts: Any) -> str:
    if isinstance(ts, pd.Timestamp):
        return ts.isoformat()
    return pd.Timestamp(ts).isoformat()


def _series_values(column: pd.Series) -> List[Optional[float]]:
    values: List[Optional[float]] = []
    for val in column:
        if pd.isna(val):
            values.append(None)
        else:
            values.append(float(val))
    return values


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
        indicators.append(
            {
                "key": spec.key,
                "label": spec.label,
                "pane": spec.pane,
                "color": spec.color,
                "values": _series_values(df[spec.key]),
            }
        )

    return {"bars": bars, "indicators": indicators}
