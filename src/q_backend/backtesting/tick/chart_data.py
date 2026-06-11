from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from q_backend.backtesting.tick.strategy import TickArrays, TickStrategy
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_brasilia_naive

_MAX_DISPLAY_BARS = 50_000

DISPLAY_TIMEFRAME_MS: Dict[str, int] = {
    "M1": 60_000,
    "M2": 120_000,
    "M3": 180_000,
    "M4": 240_000,
    "M5": 300_000,
    "M6": 360_000,
    "M10": 600_000,
    "M12": 720_000,
    "M15": 900_000,
    "M20": 1_200_000,
    "M30": 1_800_000,
    "H1": 3_600_000,
    "H2": 7_200_000,
    "H3": 10_800_000,
    "H4": 14_400_000,
    "H6": 21_600_000,
    "H8": 28_800_000,
    "H12": 43_200_000,
    "D1": 86_400_000,
    "W1": 604_800_000,
    "MN1": 2_592_000_000,
}


def _msc_to_iso(time_msc: int) -> str:
    sec = int(time_msc // 1000)
    return mt5_datetime_to_utc_iso(unix_seconds_to_brasilia_naive(sec))


def _mid_price(ticks: TickArrays) -> np.ndarray:
    last = ticks.last
    if np.any(last > 0):
        return last.astype(np.float64, copy=False)
    return (ticks.bid + ticks.ask) / 2.0


def _resolve_bar_ms(display_timeframe: str, span_msc: int) -> int:
    key = display_timeframe.upper()
    if key not in DISPLAY_TIMEFRAME_MS:
        raise ValueError(
            f"Invalid display_timeframe '{display_timeframe}'. "
            f"Choose from: {sorted(DISPLAY_TIMEFRAME_MS.keys())}"
        )
    bar_ms = DISPLAY_TIMEFRAME_MS[key]
    while span_msc > 0 and span_msc // bar_ms > _MAX_DISPLAY_BARS:
        bar_ms *= 2
        if bar_ms > DISPLAY_TIMEFRAME_MS["D1"]:
            break
    return bar_ms


def _resample_ticks_to_bars(
    ticks: TickArrays,
    bar_ms: int,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    n = len(ticks.time_msc)
    if n == 0:
        return [], []

    prices = _mid_price(ticks)
    bar_ids = ticks.time_msc // bar_ms
    boundaries = np.where(bar_ids[1:] != bar_ids[:-1])[0] + 1
    starts = np.concatenate((np.array([0], dtype=np.int64), boundaries))
    ends = np.concatenate((boundaries, np.array([n], dtype=np.int64)))

    bars: List[Dict[str, Any]] = []
    bar_ranges: List[Tuple[int, int]] = []
    for start, end in zip(starts, ends):
        slice_prices = prices[start:end]
        bar_open_msc = int(bar_ids[start] * bar_ms)
        bars.append(
            {
                "timestamp": _msc_to_iso(bar_open_msc),
                "open": float(slice_prices[0]),
                "high": float(np.max(slice_prices)),
                "low": float(np.min(slice_prices)),
                "close": float(slice_prices[-1]),
                "volume": int(np.sum(ticks.volume[start:end])),
            }
        )
        bar_ranges.append((int(start), int(end)))

    return bars, bar_ranges


def _sample_indicator_at_bars(
    series: np.ndarray,
    bar_ranges: List[Tuple[int, int]],
) -> List[Optional[float]]:
    values: List[Optional[float]] = []
    for start, end in bar_ranges:
        last_idx = end - 1
        val = series[last_idx]
        if np.isnan(val):
            values.append(None)
        else:
            values.append(float(val))
    return values


def serialize_tick_chart_data(
    ticks: TickArrays,
    strategy: TickStrategy,
    display_timeframe: str = "M1",
) -> Dict[str, Any]:
    """
  Resample ticks to display OHLCV bars and align indicator series to bar closes.
  """
    span_msc = int(ticks.time_msc[-1] - ticks.time_msc[0]) if len(ticks.time_msc) else 0
    bar_ms = _resolve_bar_ms(display_timeframe, span_msc)
    bars, bar_ranges = _resample_ticks_to_bars(ticks, bar_ms)

    indicator_series: Dict[str, np.ndarray] = {}
    compute_series = getattr(strategy, "compute_indicator_series", None)
    if compute_series is not None:
        indicator_series = compute_series(ticks)

    indicators: List[Dict[str, Any]] = []
    for spec in strategy.get_chart_indicators():
        series = indicator_series.get(spec.key)
        if series is None:
            continue
        indicators.append(
            {
                "key": spec.key,
                "label": spec.label,
                "pane": spec.pane,
                "color": spec.color,
                "values": _sample_indicator_at_bars(series, bar_ranges),
            }
        )

    return {"bars": bars, "indicators": indicators}
