"""Profile-neutral availability metadata for context nodes (WO159)."""

from __future__ import annotations

from dataclasses import dataclass

from q_backend.backtesting.session_context.compute import RUN_CONTEXT_COLUMNS


@dataclass(frozen=True)
class ContextNodeMetadata:
    availability_delay_bars: int
    required_source_columns: frozenset[str]
    uses_run_context: bool


CONTEXT_NODE_METADATA: dict[str, ContextNodeMetadata] = {
    "feature.minutes_from_open": ContextNodeMetadata(0, frozenset(), False),
    "feature.time_of_day": ContextNodeMetadata(0, frozenset(), False),
    "feature.day_of_week": ContextNodeMetadata(0, frozenset(), False),
    "feature.month_of_year": ContextNodeMetadata(0, frozenset(), False),
    "feature.session_window": ContextNodeMetadata(0, frozenset(), False),
    "feature.vol_regime": ContextNodeMetadata(0, frozenset({"close"}), False),
    "feature.trend_regime": ContextNodeMetadata(0, frozenset({"close"}), False),
    "feature.range_compression": ContextNodeMetadata(
        0, frozenset({"open", "high", "low", "close"}), False
    ),
    "feature.prev_session_high": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.prev_session_low": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.prev_session_close": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.session_gap": ContextNodeMetadata(1, frozenset({"open", "close"}), True),
    "feature.dist_prev_session_high_atr": ContextNodeMetadata(
        1, frozenset({"open", "high", "low", "close"}), True
    ),
    "feature.dist_prev_session_low_atr": ContextNodeMetadata(
        1, frozenset({"open", "high", "low", "close"}), True
    ),
    "feature.dist_prev_session_close_atr": ContextNodeMetadata(
        1, frozenset({"open", "high", "low", "close"}), True
    ),
    "feature.opening_range_high": ContextNodeMetadata(
        0, frozenset({"open", "high", "low", "close"}), False
    ),
    "feature.opening_range_low": ContextNodeMetadata(
        0, frozenset({"open", "high", "low", "close"}), False
    ),
    "feature.d1_prev_high": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.d1_prev_low": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.d1_prev_close": ContextNodeMetadata(1, frozenset({"open", "high", "low", "close"}), True),
    "feature.d1_trend": ContextNodeMetadata(1, frozenset({"close"}), True),
    "feature.d1_volatility": ContextNodeMetadata(1, frozenset({"close"}), True),
}

assert RUN_CONTEXT_COLUMNS  # import side-effect: keep bundle columns discoverable
