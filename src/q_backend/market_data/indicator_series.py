"""Market chart indicator series computation (Q-112)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException

from q_backend.market_data import api_service as market_service
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.indicator_catalog import (
    CATALOG_BY_ID,
    IndicatorEntry,
    validate_params,
)
from q_backend.market_data.models import OHLCV
from q_backend.market_data.quote_spread import open_tick_store, quote_spread, spread_vs_volatility
from q_backend.market_data.volume_at_price import value_area_levels_from_ticks
from q_backend.market_data.service import MarketDataService
from q_backend.market_data.timezone import BRASILIA_TZ, mt5_datetime_to_utc_iso
from q_backend.research.tick_store import TickStore

# A calendar window this long without a single bar is treated as the start of history.
_MIN_LOOKBACK = pd.Timedelta(days=7)
_MAX_LOOKBACK_ROUNDS = 8
_BPS = 10_000.0


@dataclass(frozen=True)
class IndicatorRequest:
    id: str
    parameters: dict[str, Any]


def _bars_to_frame(bars: list[OHLCV]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "time": [pd.Timestamp(bar.time) for bar in bars],
            "open": [float(bar.open) for bar in bars],
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [float(bar.tick_volume) for bar in bars],
        }
    )
    return frame.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def _load_frame_with_warmup(
    mds: MarketDataService,
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    warmup_bars: int,
) -> pd.DataFrame:
    """Bars of ``[start, end]`` preceded by at least ``warmup_bars`` earlier bars when they exist.

    Warm-up is counted in bars, not in calendar time: sessions, weekends and holidays leave
    gaps, so the lookback is widened until enough bars precede ``start`` or history runs out.
    """

    def fetch(window_start: pd.Timestamp, window_end: pd.Timestamp) -> list[OHLCV]:
        return market_service.fetch_ohlcv_rows(
            mds,
            symbol,
            timeframe,
            count=0,
            start=window_start.to_pydatetime(),
            end=window_end.to_pydatetime(),
        )

    if warmup_bars <= 0:
        return _bars_to_frame(fetch(start, end))

    lookback = max(_MIN_LOOKBACK, 3 * warmup_bars * bar_duration(timeframe))
    window_start = start - lookback
    bars = fetch(window_start, end)
    for _ in range(_MAX_LOOKBACK_ROUNDS):
        if sum(1 for bar in bars if pd.Timestamp(bar.time) < start) >= warmup_bars:
            break
        lookback *= 2
        earlier_start = start - lookback
        try:
            earlier = fetch(earlier_start, window_start)
        except HTTPException as exc:
            if exc.status_code not in (400, 404):
                raise
            break
        if not any(pd.Timestamp(bar.time) < window_start for bar in earlier):
            break
        bars = earlier + bars
        window_start = earlier_start
    return _bars_to_frame(bars)


def _values(series: pd.Series | np.ndarray) -> list[float | None]:
    array = np.asarray(series, dtype=np.float64)
    return [float(value) if np.isfinite(value) else None for value in array]


def _quote_values(
    entry: IndicatorEntry,
    frame: pd.DataFrame,
    params: dict[str, Any],
    *,
    symbol: str,
    timeframe: str,
    tick_store: TickStore | None,
) -> tuple[np.ndarray, str | None]:
    quotes = quote_spread(
        tick_store,
        symbol,
        frame["time"],
        timeframe,
        max_quote_age_ms=params["max_quote_age_ms"],
    )
    if entry.id == "spread_vs_volatility":
        spread = quotes.select(params["spread_output"], params["min_coverage"])
        values = spread_vs_volatility(
            frame,
            spread,
            vol_estimator=params["vol_estimator"],
            vol_window=params["vol_window"],
            vol_half_life=params["vol_half_life"],
            vol_lag=params["vol_lag"],
        ).to_numpy()
    else:
        values = quotes.select(params["output"], params["min_coverage"]) * _BPS
    return values, quotes.unavailable_reason


def build_indicator_series(
    mds: MarketDataService,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    requests: list[IndicatorRequest],
    tick_store_root: str | None = None,
) -> dict[str, Any]:
    if not requests:
        raise HTTPException(status_code=422, detail="At least one indicator request is required.")

    validated: list[tuple[IndicatorEntry, dict[str, Any]]] = []
    for req in requests:
        entry = CATALOG_BY_ID.get(req.id)
        if entry is None:
            raise HTTPException(status_code=422, detail=f"Unknown indicator identifier '{req.id}'.")
        try:
            params = validate_params(entry, req.parameters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Indicator '{req.id}': {exc}") from exc
        validated.append((entry, params))

    if not market_service.is_market_timeframe(timeframe):
        raise HTTPException(status_code=422, detail=f"Unsupported timeframe '{timeframe}'.")
    timeframe = market_service.normalize_market_timeframe(timeframe)
    try:
        bar_duration(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    warmup = max(entry.warmup_bars(params) for entry, params in validated)
    frame = _load_frame_with_warmup(mds, symbol, timeframe, start_ts, end_ts, warmup)

    in_range = ((frame["time"] >= start_ts) & (frame["time"] <= end_ts)).to_numpy()
    if not in_range.any():
        raise HTTPException(status_code=404, detail=f"No OHLCV bars for {symbol} {timeframe} in the requested range.")

    tick_store = None
    needs_ticks = any(entry.required_data in ("quotes", "trades") for entry, _ in validated)
    tick_store = open_tick_store(symbol, root=tick_store_root) if needs_ticks else None
    tick_size = None
    if any(entry.required_data == "trades" for entry, _ in validated):
        info = mds.get_symbol_info(symbol) or {}
        tick_size = float(info.get("trade_tick_size") or 0.0)
        if tick_size <= 0:
            tick_size = None

    outputs: list[dict[str, Any]] = []
    for entry, params in validated:
        if entry.required_data == "trades":
            if tick_store is None or tick_size is None:
                reason = (
                    f"The tick store is not available for {symbol}."
                    if tick_store is None
                    else f"No trade tick size is configured for {symbol}."
                )
                for output in entry.outputs:
                    payload = {
                        "key": output.key,
                        "pane": output.pane,
                        "unit": entry.unit,
                        "values": [None] * int(in_range.sum()),
                        "unavailableReason": reason,
                    }
                    outputs.append(payload)
                continue
            level_frame = frame.set_index("time")
            level_frame.index = level_frame.index.tz_localize(BRASILIA_TZ)
            levels, reason = value_area_levels_from_ticks(
                level_frame,
                tick_store,
                symbol,
                timeframe=timeframe,
                mode=params["mode"],
                tick_size=tick_size,
                row_ticks=params["row_ticks"],
                value_area=params["value_area"],
            )
            for output in entry.outputs:
                payload = {
                    "key": output.key,
                    "pane": output.pane,
                    "unit": entry.unit,
                    "values": _values(levels[output.key].to_numpy()[in_range]),
                }
                if reason is not None:
                    payload["unavailableReason"] = reason
                outputs.append(payload)
            continue
        if entry.required_data == "quotes":
            values, reason = _quote_values(
                entry, frame, params, symbol=symbol, timeframe=timeframe, tick_store=tick_store
            )
            output = entry.outputs[0]
            payload: dict[str, Any] = {
                "key": output.key,
                "pane": output.pane,
                "unit": entry.unit,
                "values": _values(values[in_range]),
            }
            if reason is not None:
                payload["unavailableReason"] = reason
            outputs.append(payload)
            continue

        computed = entry.compute(frame, params)
        for output in entry.outputs:
            outputs.append(
                {
                    "key": output.key,
                    "pane": output.pane,
                    "unit": entry.unit,
                    "values": _values(computed[output.key].to_numpy()[in_range]),
                }
            )

    timestamps = [mt5_datetime_to_utc_iso(time.to_pydatetime()) for time in frame.loc[in_range, "time"]]
    return {"timestamps": timestamps, "series": outputs}
