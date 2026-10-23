"""Typed performance analytics for a finished backtest.

Every measure is a pure function of the closed trades, the run's initial capital and its
span. Equity is the closed-trade equity curve, so open positions never contribute. Ratios
are annualised with 252 periods and a risk-free rate of 0. Percentages are fractions
(``0.1`` is 10%).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import pandas as pd

from q_backend.api.schemas.backtest import (
    BacktestPerformance,
    DrawdownPeriod,
    ExitReasonStats,
    PerformanceBasis,
    PortfolioStats,
    SideTradeStats,
    TradeStats,
)
from q_backend.backtesting.models import OrderAction, Trade

PERIODS_PER_YEAR = 252
RISK_FREE_RATE = 0.0
CALENDAR_DAYS_PER_YEAR = 365.25
DAYS_PER_MONTH = 30.4375
SECONDS_PER_DAY = 86400.0
TOP_DRAWDOWN_COUNT = 10
UNSPECIFIED_EXIT_REASON = "unspecified"


def naive_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a naive UTC timestamp; naive inputs are kept as they are."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def build_equity_curve(
    closed_trades: list[Trade],
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> pd.Series:
    equity = initial_capital
    points: list[tuple[datetime, float]] = [(naive_utc(start), equity)]

    sorted_trades = sorted(
        closed_trades,
        key=lambda trade: trade.exit_time or trade.entry_time,
    )
    for trade in sorted_trades:
        if trade.exit_time is None:
            continue
        equity += trade.pnl or 0.0
        points.append((naive_utc(trade.exit_time), equity))

    points.append((naive_utc(end), equity))

    series = pd.Series(
        [value for _, value in points],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in points]),
    )
    return series[~series.index.duplicated(keep="last")].sort_index()


def daily_equity_returns(equity: pd.Series) -> pd.Series:
    """Daily returns of an equity curve, resampled to calendar days and forward-filled."""
    daily_equity = equity.resample("D").last().ffill()
    return daily_equity.pct_change().dropna()


def daily_sharpe_ratio(daily_returns: pd.Series) -> Optional[float]:
    if len(daily_returns) < 2:
        return None
    std = daily_returns.std()
    if std == 0 or math.isnan(std):
        return None
    return float(daily_returns.mean() / std * math.sqrt(PERIODS_PER_YEAR))


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _pnl(trade: Trade) -> float:
    return trade.pnl or 0.0


def _hold_seconds(trade: Trade) -> float:
    return (naive_utc(trade.exit_time) - naive_utc(trade.entry_time)).total_seconds()


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _trade_stats(trades: list[Trade]) -> TradeStats:
    """Statistics for trades given in exit order. An empty group has null ratios and zero counts."""
    if not trades:
        return TradeStats(
            total=0,
            winning=0,
            losing=0,
            breakeven=0,
            win_rate=None,
            profit_factor=None,
            expectancy=None,
            avg_win=None,
            avg_loss=None,
            payoff_ratio=None,
            largest_win=None,
            largest_loss=None,
            max_consecutive_wins=0,
            max_consecutive_losses=0,
            avg_hold_seconds=None,
            avg_win_hold_seconds=None,
            avg_loss_hold_seconds=None,
        )

    winners = [t for t in trades if _pnl(t) > 0]
    losers = [t for t in trades if _pnl(t) <= 0]
    win_pnls = [_pnl(t) for t in winners]
    loss_pnls = [_pnl(t) for t in losers]
    gross_profit = sum(win_pnls)
    gross_loss = sum(loss_pnls)

    avg_win = _mean(win_pnls)
    avg_loss = _mean(loss_pnls)
    payoff_ratio = avg_win / abs(avg_loss) if avg_win is not None and avg_loss else None

    max_wins = max_losses = win_streak = loss_streak = 0
    for trade in trades:
        if _pnl(trade) > 0:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak += 1
            win_streak = 0
        max_wins = max(max_wins, win_streak)
        max_losses = max(max_losses, loss_streak)

    return TradeStats(
        total=len(trades),
        winning=len(winners),
        losing=len(losers),
        breakeven=sum(1 for t in trades if _pnl(t) == 0),
        win_rate=len(winners) / len(trades),
        profit_factor=gross_profit / abs(gross_loss) if gross_loss != 0 else None,
        expectancy=_mean([_pnl(t) for t in trades]),
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff_ratio,
        largest_win=max(win_pnls) if win_pnls else None,
        largest_loss=min(loss_pnls) if loss_pnls else None,
        max_consecutive_wins=max_wins,
        max_consecutive_losses=max_losses,
        avg_hold_seconds=_mean([_hold_seconds(t) for t in trades]),
        avg_win_hold_seconds=_mean([_hold_seconds(t) for t in winners]),
        avg_loss_hold_seconds=_mean([_hold_seconds(t) for t in losers]),
    )


@dataclass
class _DrawdownSpan:
    peak_time: datetime
    peak: float
    trough_time: datetime
    trough: float
    recovery_time: Optional[datetime]


def _drawdown_spans(equity: pd.Series) -> list[_DrawdownSpan]:
    """Every peak-to-recovery span of the equity curve. An equal-equity recovery ends a span."""
    spans: list[_DrawdownSpan] = []
    peak_time = equity.index[0]
    peak = float(equity.iloc[0])
    open_span: Optional[_DrawdownSpan] = None

    for timestamp, value in zip(equity.index, equity.to_numpy(dtype=float)):
        if value >= peak:
            if open_span is not None:
                open_span.recovery_time = timestamp
                spans.append(open_span)
                open_span = None
            peak_time, peak = timestamp, float(value)
        elif open_span is None:
            open_span = _DrawdownSpan(peak_time, peak, timestamp, float(value), None)
        elif value < open_span.trough:
            open_span.trough_time, open_span.trough = timestamp, float(value)

    if open_span is not None:
        spans.append(open_span)
    return spans


def _trades_in_span(trades: list[Trade], span: _DrawdownSpan, run_end: datetime) -> int:
    boundary = naive_utc(span.recovery_time) if span.recovery_time is not None else run_end
    peak = naive_utc(span.peak_time)
    return sum(1 for t in trades if peak < naive_utc(t.exit_time) <= boundary)


def _drawdown_periods(
    trades: list[Trade], equity: pd.Series
) -> tuple[list[DrawdownPeriod], float, float, float, Optional[float]]:
    """Return the deepest periods plus max value, max pct, max duration and average depth over all spans."""
    run_end = equity.index[-1]
    periods: list[DrawdownPeriod] = []
    durations: list[float] = []
    depths_pct: list[float] = []
    max_value = 0.0
    max_pct = 0.0

    for span in _drawdown_spans(equity):
        depth_value = span.peak - span.trough
        depth_pct = depth_value / span.peak if span.peak > 0 else 0.0
        end = span.recovery_time if span.recovery_time is not None else run_end
        duration_days = (end - span.peak_time).total_seconds() / SECONDS_PER_DAY
        durations.append(duration_days)
        depths_pct.append(depth_pct)
        max_value = max(max_value, depth_value)
        max_pct = max(max_pct, depth_pct)
        periods.append(
            DrawdownPeriod(
                peak_time=span.peak_time,
                trough_time=span.trough_time,
                recovery_time=span.recovery_time,
                depth_value=depth_value,
                depth_pct=depth_pct,
                duration_days=duration_days,
                trades=_trades_in_span(trades, span, run_end),
            )
        )

    periods.sort(key=lambda p: (-p.depth_pct, p.peak_time))
    avg_depth = _mean(depths_pct)
    return periods[:TOP_DRAWDOWN_COUNT], max_value, max_pct, max(durations, default=0.0), avg_depth


def _daily_ratios(equity: pd.Series) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Sharpe, Sortino and annual volatility (percent) from the daily returns, all as fractions."""
    returns = daily_equity_returns(equity).replace([math.inf, -math.inf], math.nan).dropna()
    if len(returns) < 2:
        return None, None, None

    sharpe = _finite(daily_sharpe_ratio(returns))

    downside_deviation = math.sqrt(float((returns.clip(upper=0.0) ** 2).mean()))
    sortino = None
    if downside_deviation > 0:
        sortino = _finite(returns.mean() / downside_deviation * math.sqrt(PERIODS_PER_YEAR))

    volatility = _finite(float(returns.std()) * math.sqrt(PERIODS_PER_YEAR))
    return sharpe, sortino, volatility


def _time_in_market_pct(trades: list[Trade], start: datetime, end: datetime) -> Optional[float]:
    """Share of the run span covered by at least one trade; overlapping trades count once."""
    span = (end - start).total_seconds()
    if span <= 0:
        return None

    intervals = []
    for trade in trades:
        entry = max(naive_utc(trade.entry_time), start)
        exit_ = min(naive_utc(trade.exit_time), end)
        if exit_ > entry:
            intervals.append((entry, exit_))

    covered = 0.0
    current: Optional[tuple[datetime, datetime]] = None
    for entry, exit_ in sorted(intervals):
        if current is None or entry > current[1]:
            if current is not None:
                covered += (current[1] - current[0]).total_seconds()
            current = (entry, exit_)
        else:
            current = (current[0], max(current[1], exit_))
    if current is not None:
        covered += (current[1] - current[0]).total_seconds()
    return covered / span


def _portfolio_stats(
    trades: list[Trade],
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> tuple[PortfolioStats, list[DrawdownPeriod]]:
    equity = build_equity_curve(trades, initial_capital, start, end)
    span_days = (end - start).total_seconds() / SECONDS_PER_DAY

    pnls = [_pnl(t) for t in trades]
    net_pnl = sum(pnls)
    final_equity = initial_capital + net_pnl
    return_pct = net_pnl / initial_capital if initial_capital > 0 else None

    cagr_pct = None
    if initial_capital > 0 and span_days >= 1 and final_equity > 0:
        cagr_pct = (final_equity / initial_capital) ** (CALENDAR_DAYS_PER_YEAR / span_days) - 1

    drawdowns, max_value, max_pct, max_duration, avg_depth = _drawdown_periods(trades, equity)
    sharpe, sortino, volatility = _daily_ratios(equity)

    recovery_factor = net_pnl / max_value if max_value > 0 else None
    calmar = cagr_pct / max_pct if cagr_pct is not None and max_pct > 0 else None
    trades_per_month = len(trades) / (span_days / DAYS_PER_MONTH) if span_days > 0 else None

    portfolio = PortfolioStats(
        net_pnl=net_pnl,
        gross_profit=sum(p for p in pnls if p > 0),
        gross_loss=sum(p for p in pnls if p <= 0),
        total_commission=sum(t.commission for t in trades),
        return_pct=_finite(return_pct),
        cagr_pct=_finite(cagr_pct),
        max_drawdown_value=max_value,
        max_drawdown_pct=max_pct,
        max_drawdown_duration_days=max_duration,
        avg_drawdown_pct=_finite(avg_depth),
        recovery_factor=_finite(recovery_factor),
        calmar_ratio=_finite(calmar),
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        annual_volatility_pct=volatility,
        time_in_market_pct=_finite(_time_in_market_pct(trades, naive_utc(start), naive_utc(end))),
        trades_per_month=_finite(trades_per_month),
    )
    return portfolio, drawdowns


def _exit_reason_stats(trades: list[Trade]) -> list[ExitReasonStats]:
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(trade.exit_reason or UNSPECIFIED_EXIT_REASON, []).append(trade)

    stats = [
        ExitReasonStats(
            reason=reason,
            trades=len(group),
            pnl=sum(_pnl(t) for t in group),
            win_rate=sum(1 for t in group if _pnl(t) > 0) / len(group),
        )
        for reason, group in groups.items()
    ]
    return sorted(stats, key=lambda s: (-s.trades, s.reason))


def _side_stats(trades: Iterable[Trade]) -> SideTradeStats:
    trades = list(trades)
    return SideTradeStats(
        all=_trade_stats(trades),
        long=_trade_stats([t for t in trades if t.action == OrderAction.BUY]),
        short=_trade_stats([t for t in trades if t.action == OrderAction.SELL]),
    )


def compute_backtest_performance(
    closed_trades: list[Trade],
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> BacktestPerformance:
    """Compute the typed performance of a finished run from its closed trades.

    ``start`` and ``end`` bound the run; they are normalised to naive UTC like the equity curve.
    Trades without an exit time are ignored.
    """
    trades = sorted(
        (t for t in closed_trades if t.exit_time is not None),
        key=lambda t: naive_utc(t.exit_time),
    )
    portfolio, drawdowns = _portfolio_stats(trades, initial_capital, naive_utc(start), naive_utc(end))
    return BacktestPerformance(
        basis=PerformanceBasis(
            equity="closed_trade",
            periods_per_year=PERIODS_PER_YEAR,
            risk_free_rate=RISK_FREE_RATE,
        ),
        portfolio=portfolio,
        trades=_side_stats(trades),
        drawdowns=drawdowns,
        exit_reasons=_exit_reason_stats(trades),
    )
