import math
import uuid
from abc import ABC, abstractmethod
from typing import Annotated, Literal, Optional, Union

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from q_backend.backtesting.models import (
    Signal,
    SignalAction,
    Order,
    OrderAction,
    OrderType,
)


class FixedQuantityPositionSizing(BaseModel):
    type: Literal["fixed_quantity"] = "fixed_quantity"
    quantity: float = Field(default=1.0, gt=0)
    scale_by_signal_strength: bool = Field(default=False)


class FixedSafetyMarginPositionSizing(BaseModel):
    type: Literal["fixed_safety_margin"] = "fixed_safety_margin"
    safety_margin_per_contract: float = Field(default=5000.0, gt=0)
    min_contracts: int = Field(default=1, ge=0)
    max_contracts: Optional[int] = Field(default=None, ge=0)
    scale_by_signal_strength: bool = Field(default=False)

    @model_validator(mode="after")
    def max_gte_min(self):
        if self.max_contracts is not None and self.max_contracts < self.min_contracts:
            raise ValueError("max_contracts must be >= min_contracts")
        return self


class InverseVolatilityPositionSizing(BaseModel):
    type: Literal["inverse_volatility"] = "inverse_volatility"
    target_volatility_pct: float = Field(default=10.0, gt=0)
    max_contracts: Optional[int] = Field(default=None, ge=1)
    min_contracts: int = Field(default=0, ge=0)
    scale_by_signal_strength: bool = Field(default=False)

    @model_validator(mode="after")
    def max_gte_min(self):
        if self.max_contracts is not None and self.max_contracts < self.min_contracts:
            raise ValueError("max_contracts must be >= min_contracts")
        return self


PositionSizingConfig = Annotated[
    Union[
        FixedQuantityPositionSizing,
        FixedSafetyMarginPositionSizing,
        InverseVolatilityPositionSizing,
    ],
    Field(discriminator="type"),
]


class PositionSizer(ABC):
    """
    Abstract base class for position sizing logic.
    Responsible for converting a trading Signal into an executable Order.
    """

    @abstractmethod
    def size_signal(
        self,
        signal: Signal,
        current_price: float,
        current_capital: float,
        *,
        current_data: Optional[pd.Series] = None,
    ) -> Optional[Order]:
        """
        Calculates the quantity and creates an Order based on a Signal.

        Args:
            signal: The trading signal.
            current_price: The current market price of the asset.
            current_capital: The current available capital in the backtest.
            current_data: Optional fill-bar row for sizers that need indicator columns.

        Returns:
            Order if the signal warrants trading, else None.
        """
        pass

    @abstractmethod
    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        """
        Maximum absolute position size (in units/contracts) this risk model
        permits to be open per symbol at any one time.

        The engine enforces this cap: it never lets open exposure for a symbol
        exceed this value, so repeated same-direction signals cannot pyramid
        past the configured limit. Each risk model decides how the cap is
        derived (e.g. Fixed Quantity returns its quantity; a margin-based model
        derives it from capital). Return ``None`` for an unbounded model.
        """
        pass


class FixedQuantitySizer(PositionSizer):
    """
    A basic position sizer that always trades a fixed quantity.
    """

    def __init__(self, quantity: float = 1.0, scale_by_signal_strength: bool = False):
        self.quantity = quantity
        self.scale_by_signal_strength = scale_by_signal_strength

    def size_signal(
        self,
        signal: Signal,
        current_price: float,
        current_capital: float,
        *,
        current_data: Optional[pd.Series] = None,
    ) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None

        if signal.action == SignalAction.CLOSE:
            return None

        action = (
            OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        )

        qty = self.quantity
        if self.scale_by_signal_strength:
            qty *= getattr(signal, "strength", 1.0)

        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=qty,
        )

    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        return self.quantity


class FixedSafetyMarginSizer(PositionSizer):
    """
    Futures position sizer that sizes contracts from allocated capital and a per-contract safety margin.
    """

    def __init__(
        self,
        safety_margin_per_contract: float,
        max_contracts: int | None = None,
        min_contracts: int = 1,
        scale_by_signal_strength: bool = False,
    ):
        if safety_margin_per_contract <= 0:
            raise ValueError("safety_margin_per_contract must be greater than 0")
        if min_contracts < 0:
            raise ValueError("min_contracts must be greater than or equal to 0")
        if max_contracts is not None and max_contracts < min_contracts:
            raise ValueError(
                "max_contracts must be greater than or equal to min_contracts"
            )

        self.safety_margin_per_contract = safety_margin_per_contract
        self.max_contracts = max_contracts
        self.min_contracts = min_contracts
        self.scale_by_signal_strength = scale_by_signal_strength

    def _target_contracts(self, current_capital: float) -> int:
        """Contracts this model would hold given available capital (0 if none)."""
        quantity = math.floor(current_capital / self.safety_margin_per_contract)
        if self.max_contracts is not None:
            quantity = min(quantity, self.max_contracts)

        if quantity < self.min_contracts:
            if self.min_contracts > 0 and quantity == 0:
                quantity = self.min_contracts
            else:
                return 0

        return quantity if quantity > 0 else 0

    def size_signal(
        self,
        signal: Signal,
        current_price: float,
        current_capital: float,
        *,
        current_data: Optional[pd.Series] = None,
    ) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None

        if signal.action == SignalAction.CLOSE:
            return None

        quantity = self._target_contracts(current_capital)
        if quantity <= 0:
            return None

        qty = float(quantity)
        if self.scale_by_signal_strength:
            qty = float(math.floor(qty * getattr(signal, "strength", 1.0)))
            if qty <= 0.0:
                return None

        action = (
            OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        )

        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=qty,
        )

    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        return float(self._target_contracts(current_capital))


class InverseVolatilitySizer(PositionSizer):
    """
    Volatility-targeting sizer: contracts inversely proportional to annualized vol.

    Reads annualized volatility (decimal, e.g. 0.15 for 15%) from
    ``current_data["volatility"]``. Strategies such as TSMOM expose this column;
    without it the sizer returns None (no trade).
    """

    def __init__(
        self,
        target_volatility_pct: float,
        point_value: float = 1.0,
        max_contracts: int | None = None,
        min_contracts: int = 0,
        scale_by_signal_strength: bool = False,
    ):
        if target_volatility_pct <= 0:
            raise ValueError("target_volatility_pct must be greater than 0")
        if point_value <= 0:
            raise ValueError("point_value must be greater than 0")
        if min_contracts < 0:
            raise ValueError("min_contracts must be greater than or equal to 0")
        if max_contracts is not None and max_contracts < min_contracts:
            raise ValueError(
                "max_contracts must be greater than or equal to min_contracts"
            )

        self.target_volatility_pct = target_volatility_pct
        self.point_value = point_value
        self.max_contracts = max_contracts
        self.min_contracts = min_contracts
        self.scale_by_signal_strength = scale_by_signal_strength

    def _read_volatility(self, current_data: Optional[pd.Series]) -> Optional[float]:
        if current_data is None:
            return None
        vol = current_data.get("volatility")
        if vol is None or (isinstance(vol, float) and (math.isnan(vol) or vol <= 0)):
            return None
        try:
            vol_f = float(vol)
        except (TypeError, ValueError):
            return None
        if math.isnan(vol_f) or vol_f <= 0:
            return None
        return vol_f

    def _target_contracts(
        self,
        current_price: float,
        current_capital: float,
        current_data: Optional[pd.Series],
    ) -> Optional[int]:
        vol = self._read_volatility(current_data)
        if vol is None or current_price <= 0 or current_capital <= 0:
            return None

        notional_per_contract = vol * current_price * self.point_value
        if notional_per_contract <= 0:
            return None

        raw = math.floor(
            (self.target_volatility_pct / 100.0)
            * current_capital
            / notional_per_contract
        )
        if self.max_contracts is not None:
            raw = min(raw, self.max_contracts)
        raw = max(raw, self.min_contracts)
        return raw if raw > 0 else None

    def size_signal(
        self,
        signal: Signal,
        current_price: float,
        current_capital: float,
        *,
        current_data: Optional[pd.Series] = None,
    ) -> Optional[Order]:
        if signal.action in (SignalAction.HOLD, SignalAction.CLOSE):
            return None

        quantity = self._target_contracts(current_price, current_capital, current_data)
        if quantity is None:
            return None

        qty = float(quantity)
        if self.scale_by_signal_strength:
            qty = float(math.floor(qty * getattr(signal, "strength", 1.0)))
            if qty <= 0.0:
                return None

        action = (
            OrderAction.BUY if signal.action == SignalAction.BUY else OrderAction.SELL
        )
        return Order(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            action=action,
            order_type=OrderType.MARKET,
            quantity=qty,
        )

    def max_position_size(
        self, current_price: float, current_capital: float
    ) -> Optional[float]:
        quantity = self._target_contracts(current_price, current_capital, None)
        if quantity is not None:
            return float(quantity)
        if self.max_contracts is not None:
            return float(self.max_contracts)
        return None


def build_position_sizer(
    config: Optional[
        FixedQuantityPositionSizing
        | FixedSafetyMarginPositionSizing
        | InverseVolatilityPositionSizing
    ] = None,
    *,
    point_value: float = 1.0,
) -> PositionSizer:
    if config is None:
        return FixedQuantitySizer(quantity=1.0)

    if config.type == "fixed_quantity":
        return FixedQuantitySizer(
            quantity=config.quantity,
            scale_by_signal_strength=config.scale_by_signal_strength,
        )

    if config.type == "fixed_safety_margin":
        return FixedSafetyMarginSizer(
            safety_margin_per_contract=config.safety_margin_per_contract,
            min_contracts=config.min_contracts,
            max_contracts=config.max_contracts,
            scale_by_signal_strength=config.scale_by_signal_strength,
        )

    return InverseVolatilitySizer(
        target_volatility_pct=config.target_volatility_pct,
        point_value=point_value,
        max_contracts=config.max_contracts,
        min_contracts=config.min_contracts,
        scale_by_signal_strength=config.scale_by_signal_strength,
    )
