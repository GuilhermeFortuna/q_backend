from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from q_backend.ml_filters.compatibility import compatibility_fingerprint
from q_backend.ml_filters.config import EntryFeatureConfig, EntrySample, MLFilterDataset
from q_backend.ml_filters.features import build_entry_features

_BRASILIA = ZoneInfo("America/Sao_Paulo")
_MARKET_FEATURES = (
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "real_volume",
    "e0__ma_short",
    "e0__ma_long",
    "e0__delta",
    "e0__prev_delta",
)


def _utc(value: Any) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(_BRASILIA)
    return stamp.tz_convert("UTC").to_pydatetime()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return _sha256(raw)


def _source_strategy(source_config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if source_config.get("status", "completed") != "completed":
        raise ValueError("Source backtest must be completed")
    if source_config.get("engine", "candle") != "candle":
        raise ValueError("ML filter training supports candle backtests only")
    entries = source_config.get("entries")
    if entries is None:
        if source_config.get("strategy") != "MACrossover":
            raise ValueError("Source must contain exactly one original MACrossover entry")
        params = source_config.get("strategy_params") or {}
    else:
        if len(entries) != 1 or entries[0].get("strategy") != "MACrossover":
            raise ValueError("Source must contain exactly one original MACrossover entry")
        params = entries[0].get("params") or {}
    manager = source_config.get("entry_manager") or {"kind": "or", "params": {}}
    if manager.get("kind", "or") != "or" or manager.get("params"):
        raise ValueError("Source entry manager must be 'or' with empty parameters")
    from q_backend.backtesting.strategy_registry import merge_strategy_params

    params = merge_strategy_params("MACrossover", dict(params))
    return params, str(source_config.get("source_config_revision") or "legacy-unversioned")


def _signal_matches(row: pd.Series, side: int, threshold: float) -> bool:
    delta = row.get("e0__delta", row.get("e0_delta", row.get("delta")))
    prev_delta = row.get("e0__prev_delta", row.get("e0_prev_delta", row.get("prev_delta")))
    if pd.isna(delta) or pd.isna(prev_delta):
        return False
    if side == 1:
        return bool(delta > threshold and prev_delta <= threshold)
    return bool(delta < -threshold and prev_delta >= -threshold)


def build_dataset_from_frames(
    source_run_id: str,
    config: EntryFeatureConfig,
    bars: pd.DataFrame,
    trades: pd.DataFrame,
    source_config: dict[str, Any],
    *,
    bars_checksum: str | None = None,
    trades_checksum: str | None = None,
) -> MLFilterDataset:
    """Map closed source trades to their causal signal bars and partitions."""
    params, config_revision = _source_strategy(source_config)
    market = bars.copy()
    if "time" in market.columns:
        times = pd.DatetimeIndex([_utc(v) for v in market.pop("time")])
    else:
        times = pd.DatetimeIndex([_utc(v) for v in market.index])
    if times.has_duplicates or not times.is_monotonic_increasing:
        raise ValueError("Source bars must have unique, strictly increasing timestamps")
    market.index = times
    if not {"open", "high", "low", "close"}.issubset(market.columns):
        raise ValueError("Frozen source bars are missing OHLC fields")
    if "tick_volume" not in market and "volume" in market:
        market["tick_volume"] = market["volume"]

    # Missing/all-zero real volume is absent by default. An explicitly selected
    # feature remains an error so the requested model schema cannot drift.
    selected = tuple(config.feature_names)
    if "real_volume" in selected and (
        "real_volume" not in market.columns
        or pd.to_numeric(market["real_volume"], errors="coerce").fillna(0).eq(0).all()
    ):
        raise ValueError("Selected feature 'real_volume' is unavailable or all zero in the frozen source")

    position_by_time = {stamp: pos for pos, stamp in enumerate(times)}
    rejection_counts: dict[str, dict[str, int]] = {name: {} for name in ("train", "validation", "lockbox")}
    partition_samples: dict[str, list[EntrySample]] = {name: [] for name in rejection_counts}

    def reject(partition: str, reason: str) -> None:
        bucket = rejection_counts[partition]
        bucket[reason] = bucket.get(reason, 0) + 1

    for trade in trades.to_dict(orient="records"):
        try:
            entry_time = _utc(trade["entry_time"])
        except (KeyError, TypeError, ValueError):
            # The time cutoffs cannot be inferred for malformed legacy rows.
            reject("train", "malformed_or_unclosed_trade")
            continue
        entry_position = position_by_time.get(entry_time)
        if entry_position is None or entry_position < 1:
            reject("train", "entry_time_not_in_frozen_bars")
            continue
        signal_position = entry_position - 1
        signal_time = times[signal_position].to_pydatetime()
        if signal_time < config.train_end.astimezone(timezone.utc):
            partition = "train"
        elif signal_time < config.validation_end.astimezone(timezone.utc):
            partition = "validation"
        else:
            partition = "lockbox"

        try:
            exit_value = trade.get("exit_time")
            pnl = float(trade["pnl"])
            action = str(trade.get("action", trade.get("side", ""))).upper()
        except (KeyError, TypeError, ValueError):
            reject(partition, "malformed_or_unclosed_trade")
            continue
        side = 1 if action in {"BUY", "LONG", "1"} else -1 if action in {"SELL", "SHORT", "-1"} else 0
        if exit_value is None or pd.isna(exit_value) or not pd.notna(pnl) or side == 0:
            reject(partition, "malformed_or_unclosed_trade")
            continue
        try:
            exit_time = _utc(exit_value)
        except (TypeError, ValueError):
            reject(partition, "malformed_or_unclosed_trade")
            continue
        if _signal_matches(market.iloc[signal_position], side, float(params["threshold"])) is False:
            reject(partition, "signal_side_mismatch")
            continue
        feature_frame = market.iloc[[signal_position]]
        try:
            features = build_entry_features(feature_frame, [side], selected)
        except ValueError as exc:
            reject(partition, f"missing_feature:{str(exc).split(chr(39))[1] if chr(39) in str(exc) else 'invalid'}")
            continue
        if not features.apply(pd.to_numeric, errors="coerce").notna().all(axis=None):
            reject(partition, "nonfinite_feature")
            continue
        label = int(pnl > 0.0)
        if partition == "train":
            if exit_time >= config.train_end.astimezone(timezone.utc):
                reject(partition, "trade_crosses_train_end")
            else:
                sample = EntrySample("train", signal_position, signal_time, entry_time, exit_time, side, label, pnl)
                partition_samples["train"].append(sample)
        elif partition == "validation":
            if exit_time >= config.validation_end.astimezone(timezone.utc):
                reject(partition, "trade_crosses_validation_end")
            else:
                sample = EntrySample(
                    "validation", signal_position, signal_time, entry_time, exit_time, side, label, pnl
                )
                partition_samples["validation"].append(sample)
        else:
            if exit_time > times[-1].to_pydatetime():
                reject("lockbox", "exit_after_source_coverage")
                continue
            sample = EntrySample("lockbox", signal_position, signal_time, entry_time, exit_time, side, label, pnl)
            partition_samples["lockbox"].append(sample)

    train = partition_samples["train"]
    if len(train) < 20:
        raise ValueError(f"Training requires at least 20 complete samples; found {len(train)}")
    classes = [sample.label for sample in train]
    if classes.count(0) < 2 or classes.count(1) < 2:
        raise ValueError("Training requires at least two samples from each class")
    if not partition_samples["validation"]:
        raise ValueError("Validation requires at least one complete sample")

    bar_bytes = bars.to_json(orient="split", date_format="iso").encode()
    trade_bytes = trades.to_json(orient="split", date_format="iso").encode()
    bars_digest = bars_checksum or _sha256(bar_bytes)
    trades_digest = trades_checksum or _sha256(trade_bytes)
    compatibility = compatibility_fingerprint(source_config)
    identity = _canonical_hash(
        {
            "source_run_id": source_run_id,
            "bars_checksum": bars_digest,
            "trades_checksum": trades_digest,
            "features": selected,
            "train_end": _iso_utc(config.train_end),
            "validation_end": _iso_utc(config.validation_end),
            "compatibility": compatibility,
        }
    )
    return MLFilterDataset(
        dataset_id=identity,
        source_run_id=source_run_id,
        bars=market,
        samples=tuple(sample for group in partition_samples.values() for sample in group),
        selected_features=selected,
        train_end=config.train_end,
        validation_end=config.validation_end,
        rejections=rejection_counts,
        source_config=source_config,
        source_config_revision=config_revision,
        compatibility_fingerprint=compatibility,
        bars_checksum=bars_digest,
        trades_checksum=trades_digest,
    )


def partition_features(dataset: MLFilterDataset, partition: str) -> tuple[pd.DataFrame, pd.Series, list[EntrySample]]:
    if partition not in {"train", "validation", "lockbox"}:
        raise ValueError("partition must be train, validation or lockbox")
    samples = [sample for sample in dataset.samples if sample.partition == partition]
    if not samples:
        return pd.DataFrame(columns=dataset.selected_features), pd.Series(dtype="int8"), samples
    bars = dataset.bars.iloc[[sample.signal_position for sample in samples]]
    X = build_entry_features(bars, [sample.side for sample in samples], dataset.selected_features)
    y = pd.Series([sample.label for sample in samples], index=X.index, dtype="int8")
    return X, y, samples


def _artifact_file_checksum(path: Path) -> tuple[str, tuple[int, int, int]]:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    before_signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    after_signature = (after.st_size, after.st_mtime_ns, after.st_ino)
    if before_signature != after_signature:
        raise ValueError(f"Source artifact changed while being read: {path.name}")
    return digest, before_signature


def build_source_dataset(source_run_id: str, config: EntryFeatureConfig) -> MLFilterDataset:
    """Load a source only from its immutable backtest lake artifacts."""
    import uuid

    from q_backend.storage.db.models import BacktestRun
    from q_backend.storage.db.session import session_scope
    from q_backend.storage.lake.artifacts import _artifact_absolute_path

    try:
        run_uuid = uuid.UUID(source_run_id)
    except ValueError as exc:
        raise ValueError("source_run_id must be a UUID") from exc
    with session_scope() as session:
        run = session.get(BacktestRun, run_uuid)
        if run is None:
            raise FileNotFoundError(f"Backtest source '{source_run_id}' was not found")
        if run.status != "completed":
            raise ValueError("Source backtest must be completed")
        source_config = dict(run.config)
        run_config = dict(source_config)
        run_config["status"] = run.status
        bars_path = _artifact_absolute_path(source_run_id, "market_data")
        trades_path = _artifact_absolute_path(source_run_id, "trades")
        if not bars_path.is_file() or not trades_path.is_file():
            raise FileNotFoundError("Source requires frozen market_data and trades artifacts")
        bars_digest, bars_signature = _artifact_file_checksum(bars_path)
        trades_digest, trades_signature = _artifact_file_checksum(trades_path)
        bars = pd.read_parquet(bars_path)
        trades = pd.read_parquet(trades_path)
        if _artifact_file_checksum(bars_path) != (bars_digest, bars_signature):
            raise ValueError("Source market_data artifact changed during dataset preparation")
        if _artifact_file_checksum(trades_path) != (trades_digest, trades_signature):
            raise ValueError("Source trades artifact changed during dataset preparation")
    return build_dataset_from_frames(
        source_run_id,
        config,
        bars,
        trades,
        run_config,
        bars_checksum=bars_digest,
        trades_checksum=trades_digest,
    )
