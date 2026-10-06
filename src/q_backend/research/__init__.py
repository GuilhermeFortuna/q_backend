"""Supported research helpers for loading fresh MT5 market data."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ("load_bars", "NoMarketDataError", "indicators")

if TYPE_CHECKING:
    from q_backend.research import indicators
    from q_backend.research.data import load_bars
    from q_backend.research.errors import NoMarketDataError


def __getattr__(name: str):
    if name == "load_bars":
        from q_backend.research.data import load_bars

        return load_bars
    if name == "NoMarketDataError":
        from q_backend.research.errors import NoMarketDataError

        return NoMarketDataError
    if name == "indicators":
        import importlib

        return importlib.import_module("q_backend.research.indicators")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
