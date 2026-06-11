from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from q_backend.backtesting.strategy import ChartIndicatorSpec


@dataclass
class TickArrays:
    """Columnar tick stream (WO12 ``get_ticks_columnar`` contract)."""

    time_msc: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    last: np.ndarray
    volume: np.ndarray


@dataclass
class TickSignals:
    direction: np.ndarray
    sl_points: np.ndarray
    tp_points: np.ndarray


class TickStrategy(ABC):
    """
    Abstract base for tick-native backtest strategies.

    Execution contract:

    * ``compute_signals`` must be strictly causal and fully vectorized. Index ``i``
      may depend only on ticks ``<= i``. No centered windows, no negative shifts,
      no whole-series statistics. ``test_tick_strategy_causality.py`` enforces
      this for every registered tick strategy.
    * Strategies emit aligned signal *arrays*; the intrabar kernel iterates ticks.
      There is no per-tick Python callback in this interface.
    * Indicator periods are tick-native: a ``period`` parameter counts **ticks**
      (rolling over the last N tick prices), not milliseconds. Time-window rolls
      must be derived explicitly from ``time_msc`` if needed.
  """

    def __init__(self, **kwargs):
        self.parameters = kwargs

    @abstractmethod
    def compute_signals(self, ticks: TickArrays) -> TickSignals:
        pass

    @abstractmethod
    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        pass
