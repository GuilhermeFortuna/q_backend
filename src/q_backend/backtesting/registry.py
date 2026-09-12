from typing import List, Dict, Optional
from datetime import datetime
from q_backend.backtesting.models import Order, Trade, TradeStatus, OrderAction


class TradeRegistry:
    """
    In-memory store for orders and trades during a backtest.
    Serves as the main source of truth for backtest performance calculation.
    """

    def __init__(self):
        self.orders: Dict[str, Order] = {}
        self.trades: Dict[str, Trade] = {}

    def register_order(self, order: Order) -> None:
        """Records a generated order."""
        self.orders[order.id] = order

    def register_trade(self, trade: Trade) -> None:
        """Records a new open trade."""
        self.trades[trade.id] = trade

    def close_trade(
        self, trade_id: str, exit_time: datetime, exit_price: float, exit_reason: Optional[str] = None
    ) -> Optional[Trade]:
        """
        Closes an open trade, calculates its PnL, and updates its status.
        """
        trade = self.trades.get(trade_id)
        if not trade:
            return None

        if trade.status == TradeStatus.CLOSED:
            return trade

        trade.exit_time = exit_time
        trade.exit_price = exit_price
        trade.status = TradeStatus.CLOSED
        trade.exit_reason = exit_reason

        # Calculate PnL
        # PnL = (Exit - Entry) * Quantity * Point Value for BUY
        # PnL = (Entry - Exit) * Quantity * Point Value for SELL
        multiplier = getattr(trade, "point_value", 1.0)
        if trade.action == OrderAction.BUY:
            trade.pnl = (trade.exit_price - trade.entry_price) * trade.quantity * multiplier
        else:
            trade.pnl = (trade.entry_price - trade.exit_price) * trade.quantity * multiplier

        # Subtract commissions
        trade.pnl -= trade.commission

        return trade

    def get_open_trades(self) -> List[Trade]:
        """Returns all currently open trades."""
        return [t for t in self.trades.values() if t.status == TradeStatus.OPEN]

    def get_closed_trades(self) -> List[Trade]:
        """Returns all closed trades."""
        return [t for t in self.trades.values() if t.status == TradeStatus.CLOSED]

    def get_all_trades(self) -> List[Trade]:
        """Returns the full history of trades."""
        return list(self.trades.values())

    def get_performance_metrics(self, initial_capital: float = 100000.0) -> dict:
        """
        Calculates advanced performance statistics based on closed trades.
        """
        closed_trades = self.get_closed_trades()
        total_trades = len(closed_trades)

        if total_trades == 0:
            return {
                "total_trades": 0,
                "total_pnl": 0.0,
                "total_commission": 0.0,
                "win_rate": 0.0,
                "winning_trades": 0,
                "losing_trades": 0,
                "max_drawdown_value": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "recovery_factor": 0.0,
                "expectancy": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "win_loss_ratio": 0.0,
                "max_consecutive_wins": 0,
                "max_consecutive_losses": 0,
            }

        # Sort trades by exit time safely
        def get_exit_time(t):
            if t.exit_time is None:
                return (
                    datetime.fromtimestamp(0, tz=t.entry_time.tzinfo)
                    if t.entry_time.tzinfo
                    else datetime.fromtimestamp(0)
                )
            return t.exit_time

        sorted_trades = sorted(closed_trades, key=get_exit_time)

        winning_trades = []
        losing_trades = []

        current_win_streak = 0
        current_loss_streak = 0
        max_consecutive_wins = 0
        max_consecutive_losses = 0

        for t in sorted_trades:
            pnl = t.pnl or 0.0
            if pnl > 0:
                winning_trades.append(t)
                current_win_streak += 1
                current_loss_streak = 0
                max_consecutive_wins = max(max_consecutive_wins, current_win_streak)
            else:
                losing_trades.append(t)
                current_loss_streak += 1
                current_win_streak = 0
                max_consecutive_losses = max(max_consecutive_losses, current_loss_streak)

        total_pnl = sum(t.pnl for t in closed_trades if t.pnl is not None)
        total_commission = sum(t.commission for t in closed_trades)
        gross_profit = sum(t.pnl for t in winning_trades if t.pnl is not None)
        gross_loss = sum(t.pnl for t in losing_trades if t.pnl is not None)

        # Drawdown & Equity Curve Calculation
        equity = initial_capital
        peak = initial_capital
        max_dd_val = 0.0
        max_dd_pct = 0.0

        for t in sorted_trades:
            pnl = t.pnl or 0.0
            equity += pnl
            if equity > peak:
                peak = equity
            dd_val = peak - equity
            dd_pct = dd_val / peak if peak > 0 else 0.0

            max_dd_val = max(max_dd_val, dd_val)
            max_dd_pct = max(max_dd_pct, dd_pct)

        # Ratios
        profit_factor = gross_profit / abs(gross_loss) if gross_loss != 0 else gross_profit
        recovery_factor = total_pnl / max_dd_val if max_dd_val > 0 else 0.0
        expectancy = total_pnl / total_trades

        avg_win = gross_profit / len(winning_trades) if winning_trades else 0.0
        avg_loss = gross_loss / len(losing_trades) if losing_trades else 0.0
        win_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0

        return {
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "total_commission": total_commission,
            "win_rate": len(winning_trades) / total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "max_drawdown_value": max_dd_val,
            "max_drawdown_pct": max_dd_pct,
            "profit_factor": profit_factor,
            "recovery_factor": recovery_factor,
            "expectancy": expectancy,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_loss_ratio": win_loss_ratio,
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
        }

    def merge(self, other: "TradeRegistry") -> None:
        """
        Merges another TradeRegistry's orders and trades into this one.
        Used for aggregating results from parallel chunk processing.
        """
        self.orders.update(other.orders)
        self.trades.update(other.trades)
