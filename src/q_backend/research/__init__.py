"""Supported research helpers for loading market data outside the API stack."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ("Research", "NoMarketDataError")

if TYPE_CHECKING:
    from q_backend.research.data import Research
    from q_backend.research.errors import NoMarketDataError


def __getattr__(name: str):
    if name == "Research":
        from q_backend.research.data import Research

        return Research
    if name == "NoMarketDataError":
        from q_backend.research.errors import NoMarketDataError

        return NoMarketDataError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
