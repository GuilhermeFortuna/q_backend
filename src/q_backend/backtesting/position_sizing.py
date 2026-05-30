import uuid
from abc import ABC, abstractmethod
from typing import Optional
from q_backend.backtesting.models import Signal, SignalAction, Order, OrderAction, OrderType

class PositionSizer(ABC):
    """
    Abstract base class for position sizing logic.
    Responsible for converting a trading Signal into an executable Order.
    """
    @abstractmethod
    def size_signal(self, signal: Signal, current_price: float, current_capital: float) -> Optional[Order]:
        """
        Calculates the quantity and creates an Order based on a Signal.
        
        Args:
            signal: The trading signal.
            current_price: The current market price of the asset.
            current_capital: The current available capital in the backtest.
            
        Returns:
            Order if the signal warrants trading, else None.
        """
        pass

class FixedQuantitySizer(PositionSizer):
    """
    A basic position sizer that always trades a fixed quantity.
    """
    def __init__(self, quantity: float = 1.0):
        self.quantity = quantity

    def size_signal(self, signal: Signal, current_price: float, current_capital: float) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None
            
        if signal.action == SignalAction.CLOSE:
            # We don't generate an Order for CLOSE signals right now because 
            # the engine handles CLOSE signals by closing existing open trades directly.
            # In a more advanced broker execution model, CLOSE could be an opposite market order.
            return None

        action = OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        
        # We assume Market orders for immediate execution for now
        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=self.quantity
        )
