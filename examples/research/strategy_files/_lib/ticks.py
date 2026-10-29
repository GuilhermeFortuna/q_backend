"""Shared helpers for the strategy files in this directory."""

import numpy as np


def round_up_to_tick(price, tick: float):
    """Round a price, or a series of prices, up to the next multiple of ``tick``."""
    return np.ceil(price / tick) * tick
