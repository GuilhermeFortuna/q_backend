"""Hand-computed fixtures for the typed backtest performance analytics (Q-123)."""

from __future__ import annotations

import itertools
import math
import statistics
from datetime import datetime, timedelta

import pytest

from q_backend.backtesting.analytics import compute_backtest_performance
from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.backtesting.registry import TradeRegistry

CAPITAL = 10_000.0
START = datetime(2024, 1, 1)
END = datetime(2024, 1, 31)
_ids = itertools.count()


def _trade(
    pnl: float,
    entry: datetime,
    exit_: datetime,
    action: OrderAction = OrderAction.BUY,
    reason: str | None = None,
    entry_reason: str | None = None,
) -> Trade:
    return Trade(
        id=f"trade-{next(_ids)}",
        order_id=f"order-{next(_ids)}",
        symbol="WIN$N",
        action=action,
        quantity=1.0,
        entry_time=entry,
        entry_price=100.0,
        exit_time=exit_,
        exit_price=100.0 + pnl,
        status=TradeStatus.CLOSED,
        pnl=pnl,
        entry_reason=entry_reason,
        exit_reason=reason,
    )


def _day(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour)


def _mix_trades() -> list[Trade]:
    """Long win 200 (Jan 2-4), short loss -100 (Jan 6-8), long loss -50 (Jan 10-12), short win 300 (Jan 14-20)."""
    return [
        _trade(200.0, _day(2), _day(4), OrderAction.BUY, "tp"),
        _trade(-100.0, _day(6), _day(8), OrderAction.SELL, "sl"),
        _trade(-50.0, _day(10), _day(12), OrderAction.BUY, "sl"),
        _trade(300.0, _day(14), _day(20), OrderAction.SELL, "tp"),
    ]


def _performance(trades: list[Trade], capital: float = CAPITAL):
    return compute_backtest_performance(trades, capital, START, END)


def test_empty_run_has_zero_counts_and_null_ratios():
    perf = _performance([])

    assert perf.trades.all.total == 0
    assert perf.trades.all.winning == 0
    assert perf.trades.all.win_rate is None
    assert perf.trades.all.profit_factor is None
    assert perf.trades.all.expectancy is None
    assert perf.trades.all.avg_hold_seconds is None
    assert perf.trades.long.total == 0 and perf.trades.short.total == 0
    portfolio = perf.portfolio
    assert portfolio.net_pnl == 0.0
    assert portfolio.return_pct == 0.0
    assert portfolio.cagr_pct == pytest.approx(0.0)
    assert portfolio.max_drawdown_value == 0.0
    assert portfolio.max_drawdown_duration_days == 0.0
    assert portfolio.avg_drawdown_pct is None
    assert portfolio.recovery_factor is None
    assert portfolio.calmar_ratio is None
    assert portfolio.sharpe_ratio is None
    assert portfolio.sortino_ratio is None
    assert portfolio.time_in_market_pct == 0.0
    assert portfolio.trades_per_month == 0.0
    assert perf.drawdowns == []
    assert perf.exit_reasons == []


def test_excursion_statistics_are_reported_in_money_and_resolution_counts():
    trade = _trade(80.0, _day(2), _day(2, 1))
    trade.quantity = 2
    trade.point_value = 10
    trade.mfe_points = 10
    trade.mae_points = 5
    trade.mfe_time = trade.entry_time + timedelta(seconds=20)
    trade.mfe_capture = 0.4
    trade.post_exit_return_points = 1
    trade.excursion_resolution = "minute"

    performance = compute_backtest_performance([trade], CAPITAL, START, END, post_exit_bars=4)
    stats = performance.trades.all

    assert stats.excursion_trades == 1
    assert stats.avg_mfe == 200
    assert stats.avg_mae == 100
    assert stats.mfe_capture == pytest.approx(0.4)
    assert stats.median_mfe_capture == pytest.approx(0.4)
    assert stats.median_time_to_mfe_seconds == 20
    assert stats.avg_post_exit_return == 20
    assert stats.post_exit_continuation_rate == 1
    assert performance.excursions.post_exit_bars == 4
    assert performance.excursions.resolution.minute == 1

def test_all_winners_have_null_profit_factor_and_loss_fields():
    trades = [_trade(100.0, _day(2), _day(5)), _trade(100.0, _day(6), _day(10)), _trade(100.0, _day(11), _day(20))]
    stats = _performance(trades).trades.all

    assert stats.total == 3
    assert stats.winning == 3
    assert stats.losing == 0
    assert stats.win_rate == 1.0
    assert stats.profit_factor is None
    assert stats.avg_loss is None
    assert stats.payoff_ratio is None
    assert stats.largest_loss is None
    assert stats.largest_win == 100.0
    assert stats.max_consecutive_wins == 3
    assert stats.max_consecutive_losses == 0


def test_long_and_short_split_by_side():
    perf = _performance(_mix_trades())

    assert perf.trades.long.total == 2
    assert perf.trades.long.win_rate == pytest.approx(0.5)
    assert perf.trades.long.profit_factor == pytest.approx(4.0)
    assert perf.trades.long.expectancy == pytest.approx(75.0)
    assert perf.trades.long.avg_win == pytest.approx(200.0)
    assert perf.trades.long.avg_loss == pytest.approx(-50.0)
    assert perf.trades.long.payoff_ratio == pytest.approx(4.0)
    assert perf.trades.long.largest_win == pytest.approx(200.0)
    assert perf.trades.long.largest_loss == pytest.approx(-50.0)
    assert perf.trades.long.avg_hold_seconds == pytest.approx(2 * 86400)

    assert perf.trades.short.total == 2
    assert perf.trades.short.profit_factor == pytest.approx(3.0)
    assert perf.trades.short.expectancy == pytest.approx(100.0)
    assert perf.trades.short.payoff_ratio == pytest.approx(3.0)
    assert perf.trades.short.avg_win_hold_seconds == pytest.approx(6 * 86400)
    assert perf.trades.short.avg_loss_hold_seconds == pytest.approx(2 * 86400)
    assert perf.trades.short.avg_hold_seconds == pytest.approx(4 * 86400)

    assert perf.trades.all.total == 4
    assert perf.trades.all.winning == 2
    assert perf.trades.all.losing == 2
    assert perf.trades.all.profit_factor == pytest.approx(500.0 / 150.0)
    assert perf.trades.all.expectancy == pytest.approx(87.5)
    assert perf.trades.all.avg_win == pytest.approx(250.0)
    assert perf.trades.all.avg_loss == pytest.approx(-75.0)
    assert perf.trades.all.payoff_ratio == pytest.approx(250.0 / 75.0)
    assert perf.trades.all.max_consecutive_wins == 1
    assert perf.trades.all.max_consecutive_losses == 2


def test_mix_portfolio_drawdown_and_exit_reasons():
    perf = _performance(_mix_trades())
    portfolio = perf.portfolio

    assert portfolio.net_pnl == pytest.approx(350.0)
    assert portfolio.gross_profit == pytest.approx(500.0)
    assert portfolio.gross_loss == pytest.approx(-150.0)
    assert portfolio.return_pct == pytest.approx(0.035)
    assert portfolio.max_drawdown_value == pytest.approx(150.0)
    assert portfolio.max_drawdown_pct == pytest.approx(150.0 / 10_200.0)
    assert portfolio.max_drawdown_duration_days == pytest.approx(16.0)
    assert portfolio.avg_drawdown_pct == pytest.approx(150.0 / 10_200.0)
    assert portfolio.recovery_factor == pytest.approx(350.0 / 150.0)
    # Time in market: union of 2+2+2+6 days over a 30-day span, with no overlap.
    assert portfolio.time_in_market_pct == pytest.approx(12.0 / 30.0)
    assert portfolio.trades_per_month == pytest.approx(4.0 / (30.0 / 30.4375))

    [period] = perf.drawdowns
    assert period.peak_time == _day(4)
    assert period.trough_time == _day(12)
    assert period.recovery_time == _day(20)
    assert period.depth_value == pytest.approx(150.0)
    assert period.duration_days == pytest.approx(16.0)
    assert period.trades == 3

    assert [(s.reason, s.trades, s.pnl, s.win_rate) for s in perf.exit_reasons] == [
        ("sl", 2, pytest.approx(-150.0), 0.0),
        ("tp", 2, pytest.approx(500.0), 1.0),
    ]


def test_known_daily_path_gives_sharpe_sortino_cagr_and_calmar():
    perf = _performance(_mix_trades())
    portfolio = perf.portfolio

    # Daily returns from the closed-trade equity, Jan 2 to Jan 31 (30 days), worked by hand:
    # 10000 -> 10200 (Jan 4) -> 10100 (Jan 8) -> 10050 (Jan 12) -> 10350 (Jan 20), flat otherwise.
    returns = (
        [0.0] * 2
        + [200 / 10_000]
        + [0.0] * 3
        + [-100 / 10_200]
        + [0.0] * 3
        + [-50 / 10_100]
        + [0.0] * 7
        + [300 / 10_050]
        + [0.0] * 11
    )
    assert len(returns) == 30
    mean = statistics.fmean(returns)
    std = statistics.stdev(returns)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))

    assert portfolio.sharpe_ratio == pytest.approx(mean / std * math.sqrt(252))
    assert portfolio.sortino_ratio == pytest.approx(mean / downside * math.sqrt(252))
    assert portfolio.annual_volatility_pct == pytest.approx(std * math.sqrt(252))

    cagr = (10_350.0 / 10_000.0) ** (365.25 / 30.0) - 1.0
    assert portfolio.cagr_pct == pytest.approx(cagr)
    assert portfolio.calmar_ratio == pytest.approx(cagr / (150.0 / 10_200.0))


def test_unrecovered_drawdown_runs_to_run_end():
    trades = [_trade(-300.0, _day(2), _day(5)), _trade(100.0, _day(8), _day(10))]
    perf = _performance(trades)

    [period] = perf.drawdowns
    assert period.peak_time == START
    assert period.trough_time == _day(5)
    assert period.recovery_time is None
    assert period.depth_value == pytest.approx(300.0)
    assert period.depth_pct == pytest.approx(0.03)
    assert period.duration_days == pytest.approx(30.0)
    assert period.trades == 2
    assert perf.portfolio.max_drawdown_duration_days == pytest.approx(30.0)
    assert perf.portfolio.recovery_factor == pytest.approx(-200.0 / 300.0)


def test_equal_equity_recovery_ends_the_drawdown():
    trades = [_trade(-100.0, _day(2), _day(3)), _trade(100.0, _day(5), _day(6))]
    perf = _performance(trades)

    [period] = perf.drawdowns
    assert period.recovery_time == _day(6)
    assert period.duration_days == pytest.approx(5.0)
    assert period.depth_pct == pytest.approx(0.01)


def test_drawdown_list_keeps_the_ten_deepest_periods():
    # Twelve isolated losses of increasing depth, each recovered before the next starts.
    trades = []
    for index in range(12):
        loss = 10.0 * (index + 1)
        trades.append(_trade(-loss, _day(1 + index), _day(1 + index, 6)))
        trades.append(_trade(loss, _day(1 + index, 7), _day(1 + index, 12)))
    perf = _performance(trades)

    assert len(perf.drawdowns) == 10
    depths = [period.depth_value for period in perf.drawdowns]
    assert depths == sorted(depths, reverse=True)
    assert depths[0] == pytest.approx(120.0)
    assert depths[-1] == pytest.approx(30.0)


def test_overlapping_trades_count_time_in_market_once():
    trades = [
        _trade(100.0, _day(2), _day(10)),
        _trade(50.0, _day(5), _day(8), OrderAction.SELL),
        _trade(50.0, _day(9), _day(12), OrderAction.SELL),
    ]
    portfolio = _performance(trades).portfolio

    assert portfolio.time_in_market_pct == pytest.approx(10.0 / 30.0)


def test_short_span_has_no_cagr_or_time_in_market():
    end = START + timedelta(hours=12)
    perf = compute_backtest_performance([_trade(10.0, START, START + timedelta(hours=1))], CAPITAL, START, end)

    assert perf.portfolio.cagr_pct is None
    assert perf.portfolio.trades_per_month is not None


def test_unknown_exit_reason_is_grouped_as_unspecified():
    trades = [_trade(10.0, _day(2), _day(3)), _trade(-5.0, _day(4), _day(5), reason="signal")]
    reasons = {s.reason: s for s in _performance(trades).exit_reasons}

    assert set(reasons) == {"unspecified", "signal"}
    assert reasons["unspecified"].pnl == pytest.approx(10.0)


def test_entry_reasons_group_like_exit_reasons_with_unspecified_fallback():
    trades = [
        _trade(10.0, _day(2), _day(3), entry_reason="breakout"),
        _trade(-5.0, _day(4), _day(5), entry_reason="breakout"),
        _trade(20.0, _day(6), _day(7)),
    ]
    perf = _performance(trades)

    assert [(s.reason, s.trades, s.pnl, s.win_rate) for s in perf.entry_reasons] == [
        ("breakout", 2, pytest.approx(5.0), 0.5),
        ("unspecified", 1, pytest.approx(20.0), 1.0),
    ]


def test_non_finite_values_serialize_as_null():
    perf = _performance([_trade(-100.0, _day(2), _day(3))], capital=0.0)
    payload = perf.model_dump(mode="json")

    assert payload["portfolio"]["return_pct"] is None
    assert payload["portfolio"]["cagr_pct"] is None
    assert payload["portfolio"]["sharpe_ratio"] is None


@pytest.mark.parametrize("trades", [[], _mix_trades()])
def test_shared_values_equal_the_legacy_metrics(trades):
    registry = TradeRegistry()
    for trade in trades:
        registry.register_trade(trade)
    legacy = registry.get_performance_metrics(CAPITAL)
    perf = _performance(trades)

    if not trades:
        assert legacy["total_trades"] == 0
        return
    assert perf.portfolio.max_drawdown_value == pytest.approx(legacy["max_drawdown_value"])
    assert perf.portfolio.max_drawdown_pct == pytest.approx(legacy["max_drawdown_pct"])
    assert perf.portfolio.recovery_factor == pytest.approx(legacy["recovery_factor"])
    assert perf.portfolio.net_pnl == pytest.approx(legacy["total_pnl"])
    assert perf.trades.all.win_rate == pytest.approx(legacy["win_rate"])
    assert perf.trades.all.expectancy == pytest.approx(legacy["expectancy"])
    assert perf.trades.all.max_consecutive_wins == legacy["max_consecutive_wins"]
    assert perf.trades.all.max_consecutive_losses == legacy["max_consecutive_losses"]
    assert perf.trades.all.profit_factor == pytest.approx(legacy["profit_factor"])
    assert perf.trades.all.avg_win == pytest.approx(legacy["avg_win"])
    assert perf.trades.all.avg_loss == pytest.approx(legacy["avg_loss"])
