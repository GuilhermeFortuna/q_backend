"""Supported research helpers for loading fresh MT5 market data and running strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = (
    "load_bars",
    "load_ticks",
    "resample_ticks",
    "TickStore",
    "TickSyncReport",
    "TickData",
    "sync_ticks",
    "AdjustedSeriesWarning",
    "NoMarketDataError",
    "indicators",
    "TradeOrder",
    "ResearchStrategy",
    "ResearchPosition",
    "ChartIndicator",
    "backtest",
    "BacktestResult",
    "publish_figure",
    "publish_study",
    "use_figure_style",
    "line",
    "drawdown",
    "bar",
    "histogram",
    "scatter",
    "candlestick",
    "Indicator",
    "IntParam",
    "FloatParam",
    "BoolParam",
    "ChoiceParam",
    "TimeParam",
)

if TYPE_CHECKING:
    from q_backend.research.figures import publish_figure
    from q_backend.research.studies import publish_study
    from q_backend.research.figure_style import use_figure_style
    from q_backend.research.plots import Indicator, bar, candlestick, drawdown, histogram, line, scatter
    from q_backend.research import indicators
    from q_backend.research.engine import backtest
    from q_backend.research.charting import ChartIndicator
    from q_backend.research.data import load_bars
    from q_backend.research.data import load_ticks, resample_ticks
    from q_backend.research.tick_data import TickData
    from q_backend.research.tick_store import TickStore, TickSyncReport
    from q_backend.research.errors import AdjustedSeriesWarning, NoMarketDataError
    from q_backend.research.orders import TradeOrder
    from q_backend.research.positions import ResearchPosition
    from q_backend.research.results import BacktestResult
    from q_backend.research.strategy import ResearchStrategy
    from q_backend.research.params import BoolParam, ChoiceParam, FloatParam, IntParam, TimeParam


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
    if name == "TickStore":
        from q_backend.research.tick_store import TickStore

        return TickStore
    if name == "TickData":
        from q_backend.research.tick_data import TickData

        return TickData
    if name == "TickSyncReport":
        from q_backend.research.tick_store import TickSyncReport

        return TickSyncReport
    if name == "sync_ticks":
        from q_backend.research.tick_store import sync_ticks

        return sync_ticks
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
    if name == "publish_figure":
        from q_backend.research.figures import publish_figure

        return publish_figure
    if name == "publish_study":
        from q_backend.research.studies import publish_study

        return publish_study
    if name == "use_figure_style":
        from q_backend.research.figure_style import use_figure_style

        return use_figure_style
    if name == "line":
        from q_backend.research.plots import line

        return line
    if name == "drawdown":
        from q_backend.research.plots import drawdown

        return drawdown
    if name == "bar":
        from q_backend.research.plots import bar

        return bar
    if name == "histogram":
        from q_backend.research.plots import histogram

        return histogram
    if name == "scatter":
        from q_backend.research.plots import scatter

        return scatter
    if name == "candlestick":
        from q_backend.research.plots import candlestick

        return candlestick
    if name == "Indicator":
        from q_backend.research.plots import Indicator

        return Indicator
    if name in ("IntParam", "FloatParam", "BoolParam", "ChoiceParam", "TimeParam"):
        from q_backend.research import params

        return getattr(params, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
