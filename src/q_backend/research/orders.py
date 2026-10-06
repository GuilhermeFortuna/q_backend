"""Research TradeOrder model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

OrderActionLiteral = Literal["buy", "sell", "close"]


@dataclass(frozen=True, slots=True)
class TradeOrder:
    """An immutable research decision request."""

    action: OrderActionLiteral

    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset({"buy", "sell", "close"})

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(f"Invalid TradeOrder action {self.action!r}. Must be 'buy', 'sell', or 'close'.")

    @classmethod
    def buy(cls) -> TradeOrder:
        return cls("buy")

    @classmethod
    def sell(cls) -> TradeOrder:
        return cls("sell")

    @classmethod
    def close(cls) -> TradeOrder:
        return cls("close")
