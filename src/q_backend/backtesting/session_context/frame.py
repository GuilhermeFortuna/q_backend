"""Attach run-level context columns once per evaluation frame (WO159)."""

from __future__ import annotations

import pandas as pd

from q_backend.backtesting.session_context.compute import (
    attach_context_columns,
    compute_session_context_bundle,
)
from q_backend.backtesting.session_context.config import SessionContextConfig

_PREPARED_MARKER = "_ctx_prepared"


def _coerce_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing OHLC columns so context math works on partial test frames."""
    result = df.copy()
    if "close" not in result.columns:
        raise ValueError("Session context requires a close column.")
    close = result["close"]
    if "open" not in result.columns:
        result["open"] = close
    if "high" not in result.columns:
        result["high"] = result[["open", "close"]].max(axis=1)
    if "low" not in result.columns:
        result["low"] = result[["open", "close"]].min(axis=1)
    if "volume" not in result.columns:
        result["volume"] = 0.0
    return result


def prepare_evaluation_frame(
    df: pd.DataFrame,
    *,
    timeframe: str | None = None,
    session_config: SessionContextConfig | None = None,
) -> pd.DataFrame:
    """Attach causal context columns once; safe to call repeatedly on the same frame."""
    del timeframe  # reserved for future HTF resampling rules keyed by bar size
    if _PREPARED_MARKER in df.attrs:
        return df
    config = session_config or SessionContextConfig.default_b3()
    bundle = compute_session_context_bundle(_coerce_ohlcv_columns(df), config)
    enriched = attach_context_columns(df, bundle)
    enriched.attrs[_PREPARED_MARKER] = True
    enriched.attrs["session_context_config"] = config
    return enriched
