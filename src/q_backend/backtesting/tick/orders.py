from enum import IntEnum

from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    FixedSafetyMarginPositionSizing,
    PositionSizingConfig,
)

SIZING_FIXED_QUANTITY = 0
SIZING_FIXED_SAFETY_MARGIN = 1


class ExitReason(IntEnum):
    STOP_LOSS = 1
    TAKE_PROFIT = 2
    SIGNAL = 3
    END_OF_DAY = 4


def kernel_sizing_params(
    config: PositionSizingConfig,
) -> tuple[int, float, float, float]:
    """
    Reduce a sizing config to scalars for the compiled kernel.

    Returns ``(mode, a, b, c)``:
      fixed_quantity: mode=0, a=quantity
      fixed_safety_margin: mode=1, a=margin, b=min_contracts, c=max_contracts
      (c=0 means no max cap)
    """
    if config.type == "fixed_quantity":
        return SIZING_FIXED_QUANTITY, float(config.quantity), 0.0, 0.0

    max_c = float(config.max_contracts) if config.max_contracts is not None else 0.0
    return (
        SIZING_FIXED_SAFETY_MARGIN,
        float(config.safety_margin_per_contract),
        float(config.min_contracts),
        max_c,
    )
