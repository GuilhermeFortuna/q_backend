from datetime import datetime, timezone

import pandas as pd
import pytest

from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.optimization.exit_quality import (
    score_exit_quality,
    summarize_exit_quality,
    summarize_exit_reasons,
    summarize_holding_periods,
    summarize_trade_path_quality,
)


def _trade(
    *,
    trade_id: str,
    action: OrderAction,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    exit_price: float,
    pnl: float,
    exit_reason: str | None = None,
) -> Trade:
    return Trade(
        id=trade_id,
        order_id=f"order-{trade_id}",
        symbol="TEST",
        action=action,
        quantity=1.0,
        entry_time=entry_time,
        entry_price=entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        status=TradeStatus.CLOSED,
        pnl=pnl,
        exit_reason=exit_reason,
    )


def test_summarize_exit_quality_empty_trades():
    summary = summarize_exit_quality([])

    assert summary == {
        "total_closed_trades": 0,
        "by_reason": {},
    }


def test_summarize_exit_reasons_missing_exit_reason_maps_to_unknown():
    trades = [
        _trade(
            trade_id="a",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=101.0,
            pnl=100.0,
            exit_reason=None,
        )
    ]

    by_reason = summarize_exit_reasons(trades)

    assert "unknown" in by_reason
    assert by_reason["unknown"]["trades"] == 1
    assert by_reason["unknown"]["total_pnl"] == 100.0
    assert by_reason["unknown"]["win_rate"] == 1.0


def test_summarize_exit_reasons_pnl_by_reason():
    trades = [
        _trade(
            trade_id="sl",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=95.0,
            pnl=-500.0,
            exit_reason="fixed_sl",
        ),
        _trade(
            trade_id="tp",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 3, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 4, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=110.0,
            pnl=1000.0,
            exit_reason="chandelier",
        ),
    ]

    by_reason = summarize_exit_reasons(trades)

    assert by_reason["fixed_sl"]["total_pnl"] == -500.0
    assert by_reason["fixed_sl"]["win_rate"] == 0.0
    assert by_reason["chandelier"]["total_pnl"] == 1000.0
    assert by_reason["chandelier"]["win_rate"] == 1.0


def test_summarize_trade_path_quality_normalizes_long_and_short_signs():
    bars = pd.DataFrame(
        {
            "time": pd.to_datetime(
                [
                    "2024-01-01T00:00:00Z",
                    "2024-01-02T00:00:00Z",
                    "2024-01-03T00:00:00Z",
                ],
                utc=True,
            ),
            "open": [100.0, 102.0, 98.0],
            "high": [101.0, 105.0, 99.0],
            "low": [99.0, 100.0, 95.0],
            "close": [100.5, 104.0, 96.0],
            "volume": [1000, 1000, 1000],
        }
    ).set_index("time")

    long_trade = _trade(
        trade_id="long",
        action=OrderAction.BUY,
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        exit_time=datetime(2024, 1, 3, tzinfo=timezone.utc),
        entry_price=100.0,
        exit_price=96.0,
        pnl=-400.0,
        exit_reason="signal",
    )
    short_trade = _trade(
        trade_id="short",
        action=OrderAction.SELL,
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        exit_time=datetime(2024, 1, 3, tzinfo=timezone.utc),
        entry_price=100.0,
        exit_price=96.0,
        pnl=400.0,
        exit_reason="signal",
    )

    summary = summarize_trade_path_quality([long_trade, short_trade], bars)

    assert summary["avg_mae"] < 0
    assert summary["avg_mfe_capture_ratio"] is not None
    assert summary["avg_profit_giveback"] >= 0


def test_summarize_holding_periods_with_bars():
    bars = pd.DataFrame(
        {
            "time": pd.to_datetime(
                [
                    "2024-01-01T00:00:00Z",
                    "2024-01-02T00:00:00Z",
                    "2024-01-03T00:00:00Z",
                    "2024-01-04T00:00:00Z",
                ],
                utc=True,
            ),
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0, 100.0],
            "volume": [1000, 1000, 1000, 1000],
        }
    ).set_index("time")
    trades = [
        _trade(
            trade_id="a",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=101.0,
            pnl=100.0,
        ),
        _trade(
            trade_id="b",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 4, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=101.0,
            pnl=100.0,
        ),
    ]

    summary = summarize_holding_periods(trades, bars=bars)

    assert summary["median_bars"] == 3
    assert summary["p90_bars"] == 4


def test_summarize_holding_periods_without_bars_uses_timestamps():
    trades = [
        _trade(
            trade_id="a",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=101.0,
            pnl=100.0,
        )
    ]

    summary = summarize_holding_periods(trades)

    assert summary["median_duration_seconds"] == 7200.0
    assert summary["p90_duration_seconds"] == 7200.0


def test_score_exit_quality_is_optional_and_non_ranking():
    exit_quality = {
        "path_quality": {
            "avg_mfe_capture_ratio": 0.2,
            "avg_profit_giveback": 500.0,
        },
        "by_reason": {"signal": {"total_pnl": 1000.0}},
    }

    score = score_exit_quality(
        exit_quality,
        min_mfe_capture_ratio=0.5,
        max_profit_giveback_pct=0.1,
    )

    assert score is not None
    assert score["enabled"] is True
    assert "low_mfe_capture" in score["flags"]


def test_summarize_exit_quality_omits_path_quality_without_bars():
    trades = [
        _trade(
            trade_id="a",
            action=OrderAction.BUY,
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_price=101.0,
            pnl=100.0,
            exit_reason="signal",
        )
    ]

    summary = summarize_exit_quality(trades)

    assert summary["total_closed_trades"] == 1
    assert "path_quality" not in summary
    assert "holding_period" in summary
