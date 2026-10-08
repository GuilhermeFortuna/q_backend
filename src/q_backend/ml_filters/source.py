from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from q_backend.ml_filters.dataset import _signal_matches, _source_strategy, _utc
from q_backend.ml_filters.features import FEATURE_ALLOWLIST
from q_backend.storage.db.models import BacktestOrigin, BacktestRun
from q_backend.storage.lake.artifacts import _artifact_absolute_path, read_backtest_artifact


def _closed_trade_count(trades: pd.DataFrame) -> int:
    if "exit_time" not in trades or "pnl" not in trades:
        return 0
    return int((trades["exit_time"].notna() & pd.to_numeric(trades["pnl"], errors="coerce").notna()).sum())


def describe_source(run: BacktestRun) -> dict[str, Any]:
    config = dict(run.config or {})
    errors: list[str] = []
    bars: pd.DataFrame | None = None
    trades: pd.DataFrame | None = None
    if run.origin != BacktestOrigin.STACK.value:
        errors.append(f"Backtest source is a {run.origin} run; only stack runs can be ML filter sources")
    try:
        if run.status != "completed":
            errors.append("Backtest source is not completed")
        source_config = {**config, "status": run.status}
        params, _ = _source_strategy(source_config)
        bars = read_backtest_artifact(str(run.id), "market_data")
        trades = read_backtest_artifact(str(run.id), "trades")
        if "time" not in bars.columns:
            errors.append("Frozen market_data artifact has no timestamp column")
        else:
            times = pd.DatetimeIndex([_utc(value) for value in bars["time"]])
            if times.has_duplicates or not times.is_monotonic_increasing:
                errors.append("Frozen source bars must have unique, increasing timestamps")
        if "time" in bars:
            times = pd.DatetimeIndex([_utc(value) for value in bars["time"]])
            position_by_time = {stamp: pos for pos, stamp in enumerate(times)}
            columns = set(bars.columns)
            if not {"open", "high", "low", "close"}.issubset(columns):
                errors.append("Frozen market_data artifact is missing OHLC fields")
            if not any(name in columns for name in ("e0__delta", "e0_delta", "delta")) or not any(
                name in columns for name in ("e0__prev_delta", "e0_prev_delta", "prev_delta")
            ):
                errors.append("Frozen market_data artifact is missing original MA signal columns")
            for trade in trades.to_dict(orient="records"):
                try:
                    if pd.isna(trade.get("exit_time")) or not pd.notna(float(trade.get("pnl"))):
                        continue
                    action = str(trade.get("action", trade.get("side", ""))).upper()
                    side = 1 if action in {"BUY", "LONG", "1"} else -1 if action in {"SELL", "SHORT", "-1"} else 0
                    entry_position = position_by_time.get(_utc(trade["entry_time"]))
                    if entry_position is None or entry_position < 1 or side == 0:
                        errors.append("A source trade cannot be mapped to the next open after a signal bar")
                        break
                    if not _signal_matches(bars.iloc[entry_position - 1], side, float(params["threshold"])):
                        errors.append("A source trade side does not match its original MA signal")
                        break
                except (KeyError, TypeError, ValueError):
                    errors.append("A source trade has incomplete timestamp or outcome data")
                    break
    except FileNotFoundError as exc:
        errors.append(str(exc))
    except ValueError as exc:
        errors.append(str(exc))

    available: list[str] = []
    volume_readiness = "Volume features are unavailable in the frozen source."
    split_suggestion = None
    date_start = config.get("start")
    date_end = config.get("end")
    if bars is not None and "time" in bars:
        times = pd.DatetimeIndex([_utc(value) for value in bars["time"]])
        if len(times):
            date_start = date_start or times[0].to_pydatetime()
            date_end = date_end or times[-1].to_pydatetime()
            for name in ("open", "high", "low", "close"):
                if name in bars:
                    available.append(name)
            if "tick_volume" in bars or "volume" in bars:
                available.append("tick_volume")
            real_volume = pd.to_numeric(bars.get("real_volume", pd.Series(dtype=float)), errors="coerce")
            if len(real_volume) and real_volume.notna().any() and not real_volume.fillna(0).eq(0).all():
                available.append("real_volume")
                volume_readiness = "real_volume is present with nonzero observations."
            for canonical, aliases in {
                "ma_short": ("e0__ma_short", "e0_ma_short", "ma_short"),
                "ma_long": ("e0__ma_long", "e0_ma_long", "ma_long"),
                "delta": ("e0__delta", "e0_delta", "delta"),
                "prev_delta": ("e0__prev_delta", "e0_prev_delta", "prev_delta"),
            }.items():
                if any(alias in bars for alias in aliases):
                    available.append(canonical)
            available.append("side")
            if len(times) >= 5:
                split_suggestion = {
                    "train_end": times[int(len(times) * 0.6)].to_pydatetime(),
                    "validation_end": times[int(len(times) * 0.8)].to_pydatetime(),
                }
    if "real_volume" not in available:
        volume_readiness = "real_volume is omitted because it is missing or all zero in this frozen source."
    if trades is not None and _closed_trade_count(trades) == 0:
        errors.append("Frozen source has no completed, finite-PnL trades")

    return {
        "run_id": str(run.id),
        "eligible": not errors,
        "symbol": str(config.get("symbol", "")),
        "timeframe": str(config.get("timeframe", "")),
        "strategy": str(config.get("strategy", "")),
        "date_range_start": date_start,
        "date_range_end": date_end,
        "source_sample_count": _closed_trade_count(trades) if trades is not None else 0,
        "available_features": [name for name in FEATURE_ALLOWLIST if name in available],
        "eligibility_reason": errors[0] if errors else None,
        "eligibility_errors": errors,
        "split_suggestion": split_suggestion,
        "volume_readiness": volume_readiness,
    }


def list_sources(session: Session, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    query = select(BacktestRun).where(BacktestRun.status == "completed").order_by(desc(BacktestRun.created_at))
    rows = session.execute(query.limit(limit).offset(offset)).scalars().all()
    total = session.execute(
        select(func.count()).select_from(select(BacktestRun.id).where(BacktestRun.status == "completed").subquery())
    ).scalar_one()
    return [describe_source(row) for row in rows], total


def get_source(session: Session, run_id: str) -> dict[str, Any] | None:
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        return None
    row = session.get(BacktestRun, parsed)
    return describe_source(row) if row is not None else None
