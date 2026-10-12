"""Market chart indicator series computation (Q-112)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException

from q_backend.backtesting.technical_indicators import compute_quote_spread_bars, compute_vol_normalize
from q_backend.market_data import api_service as market_service
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.indicator_catalog import (
    CATALOG_BY_ID,
    IndicatorEntry,
    catalog_entry,
    compute_indicator,
    merge_params,
    validate_params,
)
from q_backend.market_data.models import OHLCV
from q_backend.market_data.service import MarketDataService
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.errors import NoMarketDataError
from q_backend.research.tick_store import TickStore


@dataclass(frozen=True)
class IndicatorRequest:
    id: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class SeriesOutput:
    key: str
    pane: str
    unit: str | None
    values: list[float | None]
    unavailable_reason: str | None = None


def _bars_to_frame(bars: list[OHLCV]) -> pd.DataFrame:
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


def _series_to_values(series: pd.Series, times: pd.Index) -> list[float | None]:
    aligned = series.reindex(times)
    out: list[float | None] = []
    for value in aligned.to_numpy(dtype=float, na_value=np.nan):
        if np.isnan(value):
            out.append(None)
        else:
            out.append(float(value))
    return out


def _session_key(ts: pd.Timestamp) -> int:
    local = ts.tz_convert(BRASILIA_TZ)
    return local.year * 10_000 + local.month * 100 + local.day


def _bar_bounds_msc(times: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    opens: list[int] = []
    closes: list[int] = []
    for idx, ts in enumerate(times):
        if ts.tzinfo is None:
            open_ts = ts.tz_localize(BRASILIA_TZ)
        else:
            open_ts = ts.tz_convert(BRASILIA_TZ)
        if idx + 1 < len(times):
            next_ts = times.iloc[idx + 1]
            if next_ts.tzinfo is None:
                close_ts = next_ts.tz_localize(BRASILIA_TZ)
            else:
                close_ts = next_ts.tz_convert(BRASILIA_TZ)
            if close_ts.date() != open_ts.date():
                close_ts = open_ts.normalize() + pd.Timedelta(days=1)
        else:
            close_ts = open_ts.normalize() + pd.Timedelta(days=1)
        opens.append(int(open_ts.timestamp() * 1000))
        closes.append(int(close_ts.timestamp() * 1000))
    return np.asarray(opens, dtype=np.int64), np.asarray(closes, dtype=np.int64)


def _load_quote_arrays(
    store: TickStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    from q_backend.research.tick_store import _read_day_columnar

    if start.tzinfo is None:
        start = start.tz_localize(BRASILIA_TZ)
    else:
        start = start.tz_convert(BRASILIA_TZ)
    if end.tzinfo is None:
        end = end.tz_localize(BRASILIA_TZ)
    else:
        end = end.tz_convert(BRASILIA_TZ)
    start_local = start
    end_local = end
    days = store._session_days_in_range(start_local, end_local)  # noqa: SLF001 - test seam
    if not days:
        raise NoMarketDataError(
            symbol=store.symbol,
            timeframe="ticks",
            source="tick_store",
            start=start.isoformat(),
            end=end.isoformat(),
        )
    chunks: list[dict[str, np.ndarray]] = []
    for day in days:
        path = store._day_path(day)  # noqa: SLF001
        if path.is_file():
            chunks.append(_read_day_columnar(path))
    if not chunks:
        raise NoMarketDataError(
            symbol=store.symbol,
            timeframe="ticks",
            source="tick_store",
            start=start.isoformat(),
            end=end.isoformat(),
        )
    time_msc = np.concatenate([c["time_msc"] for c in chunks])
    bid = np.concatenate([c["bid"] for c in chunks])
    ask = np.concatenate([c["ask"] for c in chunks])
    flags = np.concatenate([c["flags"].astype(np.int64) for c in chunks])
    session = np.array([_session_key(start_local)] * len(time_msc), dtype=np.int64)
    order = np.argsort(time_msc, kind="stable")
    return time_msc[order], bid[order], ask[order], flags[order], session[order]


def _quote_spread_series(
    entry: IndicatorEntry,
    frame: pd.DataFrame,
    params: dict[str, Any],
    *,
    tick_store: TickStore | None,
) -> SeriesOutput:
    if tick_store is None:
        return SeriesOutput(
            key=entry.outputs[0].key,
            pane=entry.outputs[0].pane,
            unit=entry.unit,
            values=[None] * len(frame),
            unavailable_reason="Tick store is not configured for this symbol.",
        )
    times = frame["time"]
    if times.empty:
        return SeriesOutput(entry.outputs[0].key, entry.outputs[0].pane, entry.unit, [])
    try:
        time_msc, bid, ask, flags, session = _load_quote_arrays(
            tick_store,
            pd.Timestamp(times.iloc[0]),
            pd.Timestamp(times.iloc[-1]),
        )
    except NoMarketDataError as exc:
        return SeriesOutput(
            key=entry.outputs[0].key,
            pane=entry.outputs[0].pane,
            unit=entry.unit,
            values=[None] * len(times),
            unavailable_reason=str(exc),
        )
    bar_open, bar_close = _bar_bounds_msc(times)
    max_age = int(params["max_quote_age_ms"])
    last, twm, mx, coverage = compute_quote_spread_bars(
        time_msc, bid, ask, flags, session, bar_open, bar_close, max_age
    )
    output_key = params.get("output") or params.get("spread_output") or "time_weighted_mean"
    if output_key == "last":
        raw = last
    elif output_key == "max":
        raw = mx
    else:
        raw = twm
    min_cov = float(params["min_coverage"])
    values: list[float | None] = []
    for spread_bps, cov in zip(raw, coverage, strict=True):
        if np.isnan(spread_bps) or cov < min_cov:
            values.append(None)
        else:
            values.append(float(spread_bps * 10_000.0))
    return SeriesOutput(entry.outputs[0].key, entry.outputs[0].pane, entry.unit, values)


def _spread_vs_vol(entry: IndicatorEntry, frame: pd.DataFrame, params: dict[str, Any], tick_store: TickStore | None):
    spread = _quote_spread_series(entry, frame, params, tick_store=tick_store)
    if spread.unavailable_reason is not None:
        return spread
    spread_series = pd.Series(
        [np.nan if v is None else v / 10_000.0 for v in spread.values],
        index=frame["time"],
    )
    bar_frame = frame.set_index("time")
    vol_params = {k: params[k] for k in ("vol_estimator", "vol_window", "vol_half_life", "vol_lag") if k in params}
    from q_backend.market_data.indicator_catalog import compute_indicator as _compute

    vol_entry = catalog_entry(
        "per_bar_ewma_vol" if vol_params.get("vol_estimator") == "ewma" else "per_bar_realized_vol"
    )
    if vol_params.get("vol_estimator") == "ewma":
        vol_params = {"half_life": vol_params["vol_half_life"]}
    elif vol_params.get("vol_estimator") == "rogers_satchell":
        vol_entry = catalog_entry("rogers_satchell")
        vol_params = {"window": vol_params["vol_window"]}
    elif vol_params.get("vol_estimator") == "yang_zhang":
        vol_entry = catalog_entry("per_bar_yang_zhang")
        vol_params = {"window": vol_params["vol_window"]}
    else:
        vol_params = {"window": vol_params["vol_window"]}
    sigma = _compute(vol_entry, bar_frame.reset_index(), vol_params)["line"]
    sigma = pd.Series(sigma.to_numpy(), index=frame["time"])
    normalized = compute_vol_normalize(spread_series, sigma, 1, int(params["vol_lag"]))
    return SeriesOutput(
        entry.outputs[0].key,
        entry.outputs[0].pane,
        entry.unit,
        _series_to_values(normalized, frame["time"]),
    )


def _warmup_bars_for_requests(requests: list[IndicatorRequest]) -> int:
    warmup = 0
    for req in requests:
        entry = catalog_entry(req.id)
        params = merge_params(entry, req.parameters)
        warmup = max(warmup, entry.warmup_bars(params))
    return warmup


def open_tick_store(symbol: str, *, root: str | None = None) -> TickStore | None:
    """Open a TickStore for symbol and root, or return None if it cannot be opened."""
    try:
        return TickStore(symbol, root=root)
    except Exception:  # noqa: BLE001 - optional store
        return None


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
        if req.id not in CATALOG_BY_ID:
            raise HTTPException(status_code=422, detail=f"Unknown indicator identifier '{req.id}'.")
        entry = catalog_entry(req.id)
        try:
            params = validate_params(entry, req.parameters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        validated.append((entry, params))

    warmup = _warmup_bars_for_requests(requests)
    duration = bar_duration(timeframe)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    extended_start = start_ts - warmup * duration

    ohlcv = market_service.fetch_ohlcv_rows(
        mds,
        symbol,
        timeframe,
        count=5000,
        start=extended_start.to_pydatetime(),
        end=end_ts.to_pydatetime(),
    )
    frame = _bars_to_frame(ohlcv)
    if frame.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV bars for {symbol} {timeframe} in the requested range.")

    range_mask = (frame["time"] >= start_ts) & (frame["time"] <= end_ts)
    range_frame = frame.loc[range_mask].reset_index(drop=True)
    range_times = range_frame["time"]

    tick_store = None
    if any(entry.required_data == "quotes" for entry, _ in validated):
        tick_store = open_tick_store(symbol, root=tick_store_root)

    outputs: list[dict[str, Any]] = []
    for entry, params in validated:
        if entry.required_data == "quotes":
            if entry.id == "spread_vs_volatility":
                series = _spread_vs_vol(entry, range_frame, params, tick_store)
            else:
                series = _quote_spread_series(entry, range_frame, params, tick_store=tick_store)
            payload = {
                "key": series.key,
                "pane": series.pane,
                "unit": series.unit,
                "values": series.values,
            }
            if series.unavailable_reason:
                payload["unavailableReason"] = series.unavailable_reason
            outputs.append(payload)
            continue

        computed = compute_indicator(entry, frame, params)
        for out_spec in entry.outputs:
            series = computed[out_spec.key].loc[range_mask].reset_index(drop=True)
            outputs.append(
                {
                    "key": out_spec.key,
                    "pane": out_spec.pane,
                    "unit": entry.unit,
                    "values": _series_to_values(
                        pd.Series(series.to_numpy(), index=range_times),
                        range_times,
                    ),
                }
            )

    timestamps = [pd.Timestamp(t).isoformat() for t in range_times]
    return {"timestamps": timestamps, "series": outputs}
