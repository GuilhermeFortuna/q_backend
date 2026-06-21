from typing import Any, List
import pandas as pd
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec

class ExitStrategy:
    def __init__(
        self,
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        trailing_stop_pct: float = 0.0,
        stop_loss_atr: float = 0.0,
        take_profit_atr: float = 0.0,
        atr_period: int = 14,
        **kwargs,
    ):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.stop_loss_atr = stop_loss_atr
        self.take_profit_atr = take_profit_atr
        self.atr_period = atr_period
        
        # Key: trade_id -> float (highest for BUY, lowest for SELL)
        self._extreme_prices = {}

    def update_extreme_prices(self, open_trades: List[Trade], current_data: pd.Series):
        active_ids = {t.id for t in open_trades}
        # Clean up stale extreme price trackings
        self._extreme_prices = {
            tid: val for tid, val in self._extreme_prices.items() if tid in active_ids
        }
        
        current_high = current_data.get("high", current_data.get("close", 0.0))
        current_low = current_data.get("low", current_data.get("close", 0.0))
        
        for trade in open_trades:
            if trade.id not in self._extreme_prices:
                if trade.action == SignalAction.BUY or trade.action == "BUY":
                    self._extreme_prices[trade.id] = max(trade.entry_price, current_high)
                else:
                    self._extreme_prices[trade.id] = min(trade.entry_price, current_low)
            else:
                if trade.action == SignalAction.BUY or trade.action == "BUY":
                    self._extreme_prices[trade.id] = max(self._extreme_prices[trade.id], current_high)
                else:
                    self._extreme_prices[trade.id] = min(self._extreme_prices[trade.id], current_low)

    def check_exits(self, open_trades: List[Trade], current_data: pd.Series) -> List[Signal]:
        if not open_trades:
            return []
            
        self.update_extreme_prices(open_trades, current_data)
        
        signals = []
        current_close = current_data.get("close", 0.0)
        current_high = current_data.get("high", current_close)
        current_low = current_data.get("low", current_close)
        
        atr_col = f"atr_{self.atr_period}"
        atr_val = current_data.get(atr_col, None)
        # If atr_val is NaN (warm-up), we don't apply ATR exits
        if pd.isna(atr_val):
            atr_val = None
            
        for trade in open_trades:
            trigger_exit = False
            
            # --- BUY (Long) ---
            if trade.action == SignalAction.BUY or trade.action == "BUY":
                # Fixed Stop Loss (Low price checks)
                if self.stop_loss_pct > 0:
                    sl_price = trade.entry_price * (1.0 - self.stop_loss_pct)
                    if current_low <= sl_price:
                        trigger_exit = True
                
                # ATR Stop Loss
                if not trigger_exit and self.stop_loss_atr > 0 and atr_val is not None:
                    sl_price = trade.entry_price - (self.stop_loss_atr * atr_val)
                    if current_low <= sl_price:
                        trigger_exit = True
                        
                # Fixed Take Profit (High price checks)
                if not trigger_exit and self.take_profit_pct > 0:
                    tp_price = trade.entry_price * (1.0 + self.take_profit_pct)
                    if current_high >= tp_price:
                        trigger_exit = True
                
                # ATR Take Profit
                if not trigger_exit and self.take_profit_atr > 0 and atr_val is not None:
                    tp_price = trade.entry_price + (self.take_profit_atr * atr_val)
                    if current_high >= tp_price:
                        trigger_exit = True
                        
                # Trailing Stop
                if not trigger_exit and self.trailing_stop_pct > 0:
                    highest_seen = self._extreme_prices.get(trade.id, trade.entry_price)
                    trail_price = highest_seen * (1.0 - self.trailing_stop_pct)
                    if current_low <= trail_price:
                        trigger_exit = True
            
            # --- SELL (Short) ---
            elif trade.action == SignalAction.SELL or trade.action == "SELL":
                # Fixed Stop Loss (High price checks)
                if self.stop_loss_pct > 0:
                    sl_price = trade.entry_price * (1.0 + self.stop_loss_pct)
                    if current_high >= sl_price:
                        trigger_exit = True
                
                # ATR Stop Loss
                if not trigger_exit and self.stop_loss_atr > 0 and atr_val is not None:
                    sl_price = trade.entry_price + (self.stop_loss_atr * atr_val)
                    if current_high >= sl_price:
                        trigger_exit = True
                        
                # Fixed Take Profit (Low price checks)
                if not trigger_exit and self.take_profit_pct > 0:
                    tp_price = trade.entry_price * (1.0 - self.take_profit_pct)
                    if current_low <= tp_price:
                        trigger_exit = True
                
                # ATR Take Profit
                if not trigger_exit and self.take_profit_atr > 0 and atr_val is not None:
                    tp_price = trade.entry_price - (self.take_profit_atr * atr_val)
                    if current_low <= tp_price:
                        trigger_exit = True
                        
                # Trailing Stop
                if not trigger_exit and self.trailing_stop_pct > 0:
                    lowest_seen = self._extreme_prices.get(trade.id, trade.entry_price)
                    trail_price = lowest_seen * (1.0 + self.trailing_stop_pct)
                    if current_high >= trail_price:
                        trigger_exit = True
                        
            if trigger_exit:
                signals.append(Signal(symbol=trade.symbol, action=SignalAction.CLOSE))
                
        return signals

def get_exit_strategy_params() -> List[StrategyParamSpec]:
    return [
        StrategyParamSpec(
            name="stop_loss_pct",
            label="Stop Loss (%)",
            type="float",
            default=0.0,
            min=0.0,
            max=0.50,
            step=0.001,
            hint="Fixed stop loss percentage from entry price (e.g. 0.02 = 2%). 0.0 to disable.",
        ),
        StrategyParamSpec(
            name="take_profit_pct",
            label="Take Profit (%)",
            type="float",
            default=0.0,
            min=0.0,
            max=1.0,
            step=0.001,
            hint="Fixed take profit percentage from entry price (e.g. 0.05 = 5%). 0.0 to disable.",
        ),
        StrategyParamSpec(
            name="trailing_stop_pct",
            label="Trailing Stop (%)",
            type="float",
            default=0.0,
            min=0.0,
            max=0.50,
            step=0.001,
            hint="Trailing stop percentage from peak price (e.g. 0.02 = 2%). 0.0 to disable.",
        ),
        StrategyParamSpec(
            name="stop_loss_atr",
            label="Stop Loss (ATR Mult)",
            type="float",
            default=0.0,
            min=0.0,
            max=10.0,
            step=0.1,
            hint="Stop loss as a multiple of ATR from entry price. 0.0 to disable.",
        ),
        StrategyParamSpec(
            name="take_profit_atr",
            label="Take Profit (ATR Mult)",
            type="float",
            default=0.0,
            min=0.0,
            max=20.0,
            step=0.1,
            hint="Take profit as a multiple of ATR from entry price. 0.0 to disable.",
        ),
        StrategyParamSpec(
            name="atr_period",
            label="ATR Period",
            type="int",
            default=14,
            min=2,
            max=100,
            step=1,
            hint="Period for ATR calculation used by ATR exits.",
        ),
    ]
