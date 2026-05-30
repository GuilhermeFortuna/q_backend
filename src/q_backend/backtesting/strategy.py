from abc import ABC, abstractmethod
from typing import List
import pandas as pd
from q_backend.backtesting.models import Signal, Trade

class TradingStrategy(ABC):
    """
    Abstract base class for all trading strategies in the backtesting engine.
    Strategies are responsible for analyzing data and emitting Signals.
    """
    
    def __init__(self, **kwargs):
        """
        Initialize strategy parameters.
        """
        self.parameters = kwargs

    @abstractmethod
    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Computes necessary technical indicators for the strategy.
        Typically receives historical market data and returns it with new indicator columns.
        
        Args:
            data (pd.DataFrame): Historical market data (OHLCV or tick).
            
        Returns:
            pd.DataFrame: Data augmented with technical indicators.
        """
        pass

    @abstractmethod
    def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
        """
        Evaluates entry conditions for a given point in time and returns a list of Signals 
        if conditions are met.
        
        Args:
            current_data (pd.Series): A single row (e.g. current candle) of data.
            
        Returns:
            List[Signal]: A list of entry Signals (BUY, SELL).
        """
        pass

    @abstractmethod
    def check_exit_conditions(self, current_data: pd.Series, open_trades: List[Trade]) -> List[Signal]:
        """
        Evaluates exit conditions for currently open trades. 
        Returns CLOSE Signals if exit conditions (e.g., stop loss, take profit, indicator cross) are met.
        
        Args:
            current_data (pd.Series): A single row (e.g. current candle) of data.
            open_trades (List[Trade]): Trades currently open in the registry.
            
        Returns:
            List[Signal]: A list of CLOSE Signals.
        """
        pass
