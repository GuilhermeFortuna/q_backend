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

    def close_trade(self, trade_id: str, exit_time: datetime, exit_price: float) -> Optional[Trade]:
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

        # Calculate PnL
        # PnL = (Exit - Entry) * Quantity for BUY
        # PnL = (Entry - Exit) * Quantity for SELL
        if trade.action == OrderAction.BUY:
            trade.pnl = (trade.exit_price - trade.entry_price) * trade.quantity
        else:
            trade.pnl = (trade.entry_price - trade.exit_price) * trade.quantity

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

    def get_performance_metrics(self) -> dict:
        """
        Calculates basic performance statistics based on closed trades.
        """
        closed_trades = self.get_closed_trades()
        total_trades = len(closed_trades)
        
        if total_trades == 0:
            return {
                "total_trades": 0,
                "total_pnl": 0.0,
                "win_rate": 0.0,
                "winning_trades": 0,
                "losing_trades": 0,
            }

        winning_trades = [t for t in closed_trades if t.pnl is not None and t.pnl > 0]
        losing_trades = [t for t in closed_trades if t.pnl is not None and t.pnl <= 0]
        
        total_pnl = sum(t.pnl for t in closed_trades if t.pnl is not None)
        
        return {
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "win_rate": len(winning_trades) / total_trades if total_trades > 0 else 0.0,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
        }

    def merge(self, other: 'TradeRegistry') -> None:
        """
        Merges another TradeRegistry's orders and trades into this one.
        Used for aggregating results from parallel chunk processing.
        """
        self.orders.update(other.orders)
        self.trades.update(other.trades)
