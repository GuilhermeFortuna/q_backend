"""Backtest-time compatibility rules between an ML entry filter and its model.

Everything here is pure (no database or artifact access) so the API, the worker and
the unsupported-workflow guards share one definition of "compatible".
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ML_FILTER_STRATEGY = "MACrossoverMLFilter"
ORIGINAL_STRATEGY = "MACrossover"

# Settings that must match the model's training source (Q-086 dataset fingerprint).
COMPATIBILITY_KEYS = (
    "symbol",
    "timeframe",
    "strategy",
    "strategy_params",
    "entries",
    "entry_manager",
    "exit_params",
    "day_trade",
    "day_trade_start_time",
    "day_trade_end_time",
    "day_trade_close_time",
    "costs",
    "point_value",
    "position_sizing",
    "initial_capital",
)


class MLFilterRequestError(ValueError):
    """The backtest request is not a valid ML-filter composition (HTTP 422)."""


class MLFilterCompatibilityError(ValueError):
    """The selected model does not match the requested backtest (HTTP 409)."""


class MLFilterModelUnavailableError(FileNotFoundError):
    """The pinned model version is missing, not ready, or cannot be read (HTTP 404)."""


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def compatibility_fingerprint(source_config: Mapping[str, Any]) -> str:
    """Fingerprint of the execution settings an ML filter model was trained under."""
    return canonical_hash({key: source_config.get(key) for key in COMPATIBILITY_KEYS})


def _entry_names(config: Mapping[str, Any]) -> list[str]:
    entries = config.get("entries")
    if entries is None:
        return [str(config.get("strategy", ORIGINAL_STRATEGY))]
    return [str(entry.get("strategy")) for entry in entries]


def uses_ml_filter(config: Mapping[str, Any]) -> bool:
    return config.get("ml_filter") is not None or ML_FILTER_STRATEGY in _entry_names(config)


def validate_filter_request_shape(config: Mapping[str, Any]) -> None:
    """Reject unsupported compositions before any model is resolved.

    ``config`` is the dumped backtest request. Filterless requests for any other
    strategy are always valid and never inspected further.
    """
    if not uses_ml_filter(config):
        return
    names = _entry_names(config)
    filter_config = config.get("ml_filter")
    if filter_config is None:
        raise MLFilterRequestError(f"{ML_FILTER_STRATEGY} requires an ml_filter model version and threshold")
    if names != [ML_FILTER_STRATEGY]:
        if len(names) != 1:
            raise MLFilterRequestError("ml_filter supports exactly one MA Crossover · ML Filter entry")
        raise MLFilterRequestError(
            f"ml_filter cannot be combined with the {names[0]} strategy; select {ML_FILTER_STRATEGY} instead"
        )
    if config.get("engine", "candle") != "candle":
        raise MLFilterRequestError("ml_filter backtests support the candle engine only")
    manager = config.get("entry_manager") or {}
    if manager.get("kind", "or") != "or" or manager.get("params"):
        raise MLFilterRequestError("ml_filter requires the 'or' entry manager with empty parameters")
    threshold = filter_config.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise MLFilterRequestError("ml_filter threshold must be a finite number in [0, 1]")
    if not 0.0 <= float(threshold) <= 1.0:
        raise MLFilterRequestError("ml_filter threshold must be a finite number in [0, 1]")


def source_equivalent_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the request as the original MACrossover source it filters."""
    equivalent = {key: config.get(key) for key in COMPATIBILITY_KEYS}
    if equivalent["strategy"] == ML_FILTER_STRATEGY:
        equivalent["strategy"] = ORIGINAL_STRATEGY
    if equivalent["entries"] is not None:
        equivalent["entries"] = [
            {**entry, "strategy": ORIGINAL_STRATEGY} if entry.get("strategy") == ML_FILTER_STRATEGY else entry
            for entry in equivalent["entries"]
        ]
    return equivalent


def to_utc(value: datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(ZoneInfo("America/Sao_Paulo"))
    return stamp.tz_convert("UTC")


def requested_window(config: Mapping[str, Any], *, now: datetime | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve the requested range with the same defaults the worker applies."""
    current = now or datetime.now().astimezone()
    start = config.get("start") or current - timedelta(days=365)
    end = config.get("end") or current
    return to_utc(pd.Timestamp(start).to_pydatetime()), to_utc(pd.Timestamp(end).to_pydatetime())


def validate_filter_compatibility(
    config: Mapping[str, Any],
    model_manifest: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    """Fail with a 409-class error unless the model fits this exact backtest.

    ``model_manifest`` carries the published model manifest merged with its dataset
    manifest fields (``compatibility_fingerprint`` and ``train_end``). Nothing is
    adapted silently: mismatched costs, sizing, MA or exit settings are errors.
    """
    validate_filter_request_shape(config)
    if model_manifest.get("model_version_id") != (config.get("ml_filter") or {}).get("model_version_id"):
        raise MLFilterCompatibilityError("Model manifest does not belong to the requested model version")
    expected = model_manifest.get("compatibility_fingerprint")
    if compatibility_fingerprint(source_equivalent_config(config)) != expected:
        raise MLFilterCompatibilityError(
            "The backtest's symbol, timeframe, MA, exit, cost, sizing or day-trade settings differ from the "
            "source this model was trained on"
        )
    train_end = to_utc(datetime.fromisoformat(str(model_manifest["train_end"])))
    _start, end = requested_window(config, now=now)
    if end <= train_end:
        raise MLFilterCompatibilityError(
            f"The requested range ends at {end.isoformat()}, not after the model's train_end "
            f"{train_end.isoformat()}; trading dates must be at or after train_end"
        )
