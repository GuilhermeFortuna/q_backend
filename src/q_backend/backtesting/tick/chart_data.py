from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from q_backend.backtesting.tick.kernel import bars, resolve_bar_ms, sample_at_bar_ends
from q_backend.backtesting.tick.strategy import TickArrays, TickStrategy
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_brasilia_naive

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


def _resolve_bar_ms(display_timeframe: str, span_msc: int) -> int:
    key = display_timeframe.upper()
    if key not in DISPLAY_TIMEFRAME_MS:
        raise ValueError(
            f"Invalid display_timeframe '{display_timeframe}'. " f"Choose from: {sorted(DISPLAY_TIMEFRAME_MS.keys())}"
        )
    return resolve_bar_ms(DISPLAY_TIMEFRAME_MS[key], span_msc)


def _resample_ticks_to_bars(
    ticks: TickArrays,
    bar_ms: int,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    n = len(ticks.time_msc)
    if n == 0:
        return [], []

    arrays = bars(ticks, bar_ms)

    open_msc = arrays["open_msc"]
    opens = arrays["open"]
    highs = arrays["high"]
    lows = arrays["low"]
    closes = arrays["close"]
    volumes = arrays["volume"]
    tick_start = arrays["tick_start"]
    tick_end = arrays["tick_end"]

    bar_list: List[Dict[str, Any]] = []
    bar_ranges: List[Tuple[int, int]] = []
    for i in range(len(open_msc)):
        bar_list.append(
            {
                "timestamp": _msc_to_iso(int(open_msc[i])),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": int(volumes[i]),
            }
        )
        bar_ranges.append((int(tick_start[i]), int(tick_end[i])))

    return bar_list, bar_ranges


def _sample_indicator_at_bars(
    series: np.ndarray,
    bar_ranges: List[Tuple[int, int]],
) -> List[Optional[float]]:
    if not bar_ranges:
        return []
    tick_end = np.asarray([end for _, end in bar_ranges], dtype=np.int64)
    sampled = sample_at_bar_ends(series, tick_end)
    return [None if np.isnan(val) else float(val) for val in sampled]


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
    bars_out, bar_ranges = _resample_ticks_to_bars(ticks, bar_ms)

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

    return {"bars": bars_out, "indicators": indicators}
