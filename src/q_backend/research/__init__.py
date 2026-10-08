"""Supported research helpers for loading fresh MT5 market data and running strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = (
    "load_bars",
    "load_ticks",
    "resample_ticks",
    "AdjustedSeriesWarning",
    "NoMarketDataError",
    "indicators",
    "TradeOrder",
    "ResearchStrategy",
    "ResearchPosition",
    "ChartIndicator",
    "backtest",
    "BacktestResult",
)

if TYPE_CHECKING:
    from q_backend.research import indicators
    from q_backend.research.engine import backtest
    from q_backend.research.charting import ChartIndicator
    from q_backend.research.data import load_bars
    from q_backend.research.data import load_ticks, resample_ticks
    from q_backend.research.errors import AdjustedSeriesWarning, NoMarketDataError
    from q_backend.research.orders import TradeOrder
    from q_backend.research.positions import ResearchPosition
    from q_backend.research.results import BacktestResult
    from q_backend.research.strategy import ResearchStrategy


def __getattr__(name: str):
    if name == "load_bars":
        from q_backend.research.data import load_bars

        return load_bars
    if name == "load_ticks":
        from q_backend.research.data import load_ticks

        return load_ticks
    if name == "resample_ticks":
        from q_backend.research.data import resample_ticks

        return resample_ticks
    if name == "AdjustedSeriesWarning":
        from q_backend.research.errors import AdjustedSeriesWarning

        return AdjustedSeriesWarning
    if name == "NoMarketDataError":
        from q_backend.research.errors import NoMarketDataError

        return NoMarketDataError
    if name == "indicators":
        import importlib

        return importlib.import_module("q_backend.research.indicators")
    if name == "TradeOrder":
        from q_backend.research.orders import TradeOrder

        return TradeOrder
    if name == "ChartIndicator":
        from q_backend.research.charting import ChartIndicator

        return ChartIndicator
    if name == "ResearchPosition":
        from q_backend.research.positions import ResearchPosition

        return ResearchPosition
    if name == "ResearchStrategy":
        from q_backend.research.strategy import ResearchStrategy

        return ResearchStrategy
    if name == "backtest":
        from q_backend.research.engine import backtest

        return backtest
    if name == "BacktestResult":
        from q_backend.research.results import BacktestResult

        return BacktestResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
