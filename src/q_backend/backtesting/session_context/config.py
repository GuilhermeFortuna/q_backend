"""Run-level session configuration for B3 context features (WO159)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContextConfig:
    """Explicit session parameters attached once per backtest/discovery run."""

    session_open: str = "09:00"
    session_close: str = "18:00"
    opening_range_minutes: int = 60
    d1_trend_lookback: int = 5
    d1_vol_window: int = 21

    @classmethod
    def default_b3(cls) -> SessionContextConfig:
        return cls()

    @classmethod
    def from_day_trade_times(
        cls,
        *,
        start_time: str,
        end_time: str,
        opening_range_minutes: int = 60,
    ) -> SessionContextConfig:
        return cls(
            session_open=start_time,
            session_close=end_time,
            opening_range_minutes=opening_range_minutes,
        )
