"""Exit-quality analytics for stitched out-of-sample trades."""

from __future__ import annotations

from statistics import median
from typing import Any

import pandas as pd

from q_backend.backtesting.models import OrderAction, Trade, TradeStatus


def _normalize_trades(trades: list[Trade] | pd.DataFrame) -> list[dict[str, Any]]:
    if isinstance(trades, pd.DataFrame):
        if trades.empty:
            return []
        records = trades.to_dict(orient="records")
    else:
        records = [trade.model_dump() if hasattr(trade, "model_dump") else dict(trade) for trade in trades]

    closed: list[dict[str, Any]] = []
    for record in records:
        status = record.get("status")
        if status is not None and status != TradeStatus.CLOSED.value:
            continue
        if record.get("exit_time") is None or record.get("exit_price") is None:
            continue
        closed.append(record)
    return closed


def _exit_reason_key(record: dict[str, Any]) -> str:
    reason = record.get("exit_reason")
    if reason is None or (isinstance(reason, str) and not reason.strip()):
        return "unknown"
    return str(reason)


def _percentile(values: list[float | int], pct: float) -> float | int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_exit_reasons(trades: list[Trade] | pd.DataFrame) -> dict[str, Any]:
    closed = _normalize_trades(trades)
    by_reason: dict[str, dict[str, Any]] = {}

    for record in closed:
        reason = _exit_reason_key(record)
        bucket = by_reason.setdefault(
            reason,
            {"trades": 0, "total_pnl": 0.0, "wins": 0},
        )
        pnl = float(record.get("pnl") or 0.0)
        bucket["trades"] += 1
        bucket["total_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    summary: dict[str, dict[str, Any]] = {}
    for reason, bucket in sorted(by_reason.items()):
        trades_count = bucket["trades"]
        summary[reason] = {
            "trades": trades_count,
            "total_pnl": bucket["total_pnl"],
            "win_rate": bucket["wins"] / trades_count if trades_count else 0.0,
        }
    return summary


def _bars_frame(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    if "time" in frame.columns:
        frame = frame.set_index("time")
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, format="ISO8601", utc=True)
    else:
        frame.index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index
    frame = frame.sort_index()
    return frame


def _holding_bars(record: dict[str, Any], bars: pd.DataFrame) -> int | None:
    entry_time = pd.Timestamp(record["entry_time"])
    exit_time = pd.Timestamp(record["exit_time"])
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")
    if exit_time.tzinfo is None:
        exit_time = exit_time.tz_localize("UTC")

    frame = _bars_frame(bars)
    in_trade = frame.index[(frame.index >= entry_time) & (frame.index <= exit_time)]
    if len(in_trade) == 0:
        return None
    return max(int(len(in_trade)), 1)


def _holding_duration_seconds(record: dict[str, Any]) -> float | None:
    entry_time = pd.Timestamp(record["entry_time"])
    exit_time = pd.Timestamp(record["exit_time"])
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")
    if exit_time.tzinfo is None:
        exit_time = exit_time.tz_localize("UTC")
    seconds = (exit_time - entry_time).total_seconds()
    return max(seconds, 0.0)


def summarize_holding_periods(
    trades: list[Trade] | pd.DataFrame,
    bars: pd.DataFrame | None = None,
) -> dict[str, Any]:
    closed = _normalize_trades(trades)
    if not closed:
        return {}

    if bars is not None and not bars.empty:
        bar_counts = [count for record in closed if (count := _holding_bars(record, bars)) is not None]
        if not bar_counts:
            return {}
        return {
            "median_bars": int(round(median(bar_counts))),
            "p90_bars": int(round(_percentile(bar_counts, 0.9) or 0)),
        }

    durations = [duration for record in closed if (duration := _holding_duration_seconds(record)) is not None]
    if not durations:
        return {}
    return {
        "median_duration_seconds": float(median(durations)),
        "p90_duration_seconds": float(_percentile(durations, 0.9) or 0.0),
    }


def _trade_bars(record: dict[str, Any], bars: pd.DataFrame) -> pd.DataFrame:
    entry_time = pd.Timestamp(record["entry_time"])
    exit_time = pd.Timestamp(record["exit_time"])
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")
    if exit_time.tzinfo is None:
        exit_time = exit_time.tz_localize("UTC")
    frame = _bars_frame(bars)
    return frame.loc[(frame.index >= entry_time) & (frame.index <= exit_time)]


def _normalized_mae_mfe(
    record: dict[str, Any],
    trade_bars: pd.DataFrame,
) -> tuple[float | None, float | None]:
    if trade_bars.empty:
        return None, None

    entry_price = float(record["entry_price"])
    high = float(trade_bars["high"].max())
    low = float(trade_bars["low"].min())
    action = record.get("action")

    if action == OrderAction.BUY.value:
        mfe = high - entry_price
        mae = low - entry_price
    elif action == OrderAction.SELL.value:
        mfe = entry_price - low
        mae = entry_price - high
    else:
        return None, None

    return mae, max(mfe, 0.0)


def summarize_trade_path_quality(
    trades: list[Trade] | pd.DataFrame,
    bars: pd.DataFrame,
) -> dict[str, Any]:
    closed = _normalize_trades(trades)
    if not closed or bars is None or bars.empty:
        return {}

    mfe_capture_ratios: list[float] = []
    profit_givebacks: list[float] = []
    mae_values: list[float] = []

    for record in closed:
        trade_bars = _trade_bars(record, bars)
        mae, mfe = _normalized_mae_mfe(record, trade_bars)
        if mae is None or mfe is None:
            continue

        quantity = float(record.get("quantity") or 0.0)
        point_value = float(record.get("point_value") or 1.0)
        pnl = float(record.get("pnl") or 0.0)
        mfe_dollars = mfe * quantity * point_value
        mae_dollars = mae * quantity * point_value

        mae_values.append(mae_dollars)
        if mfe_dollars > 0:
            mfe_capture_ratios.append(pnl / mfe_dollars)
        if mfe_dollars > pnl:
            profit_givebacks.append(mfe_dollars - pnl)

    if not mae_values and not mfe_capture_ratios and not profit_givebacks:
        return {}

    summary: dict[str, Any] = {}
    if mfe_capture_ratios:
        summary["avg_mfe_capture_ratio"] = sum(mfe_capture_ratios) / len(mfe_capture_ratios)
    if profit_givebacks:
        summary["avg_profit_giveback"] = sum(profit_givebacks) / len(profit_givebacks)
    if mae_values:
        summary["avg_mae"] = sum(mae_values) / len(mae_values)
    return summary


def summarize_exit_quality(
    trades: list[Trade] | pd.DataFrame,
    bars: pd.DataFrame | None = None,
) -> dict[str, Any]:
    closed = _normalize_trades(trades)
    summary: dict[str, Any] = {
        "total_closed_trades": len(closed),
        "by_reason": summarize_exit_reasons(trades),
    }

    holding = summarize_holding_periods(trades, bars=bars)
    if holding:
        summary["holding_period"] = holding

    if bars is not None and not bars.empty:
        path_quality = summarize_trade_path_quality(trades, bars)
        if path_quality:
            summary["path_quality"] = path_quality

    return summary


def score_exit_quality(
    exit_quality: dict[str, Any],
    *,
    min_mfe_capture_ratio: float | None = None,
    max_profit_giveback_pct: float | None = None,
) -> dict[str, Any] | None:
    """Optional soft score for experimentation; does not affect ranking by default."""
    path_quality = exit_quality.get("path_quality") or {}
    score: dict[str, Any] = {"enabled": True}
    flags: list[str] = []

    capture = path_quality.get("avg_mfe_capture_ratio")
    if min_mfe_capture_ratio is not None and capture is not None:
        passed = capture >= min_mfe_capture_ratio
        score["mfe_capture_passed"] = passed
        if not passed:
            flags.append("low_mfe_capture")

    giveback = path_quality.get("avg_profit_giveback")
    total_pnl = sum(bucket.get("total_pnl", 0.0) for bucket in (exit_quality.get("by_reason") or {}).values())
    if max_profit_giveback_pct is not None and giveback is not None and total_pnl > 0:
        threshold = total_pnl * max_profit_giveback_pct
        passed = giveback <= threshold
        score["profit_giveback_passed"] = passed
        if not passed:
            flags.append("high_profit_giveback")

    if flags:
        score["flags"] = flags
    if len(score) == 1:
        return None
    return score
