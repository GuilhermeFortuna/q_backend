"""Neutral helpers for columnar tick arrays shared across market-data providers.

Extracted (WO184) from ``clients/local.py`` so both ``LocalParquetClient`` and
``RemoteMt5Client`` derive ``list[Tick]`` from the columnar dict the exact same way,
rather than each duplicating the row-building loop. The columnar layout is the
``COLUMNAR_TICK_KEYS`` contract produced by ``_empty_ticks_columnar`` /
``get_ticks_columnar``.
"""

from __future__ import annotations

import numpy as np

from q_backend.market_data.clients.metatrader import _time_msc_to_naive_local
from q_backend.market_data.models import Tick


def columnar_to_ticks(arrays: dict[str, np.ndarray]) -> list[Tick]:
    """Convert columnar tick arrays into ordered ``Tick`` models.

    ``time`` is reconstructed from ``time_msc`` with the existing naive-Brasília
    convention (no new timezone code). Returns ``[]`` for an empty range.
    """
    count = len(arrays["time_msc"])
    if count == 0:
        return []
    return [
        Tick(
            time=_time_msc_to_naive_local(int(arrays["time_msc"][i])),
            bid=float(arrays["bid"][i]),
            ask=float(arrays["ask"][i]),
            last=float(arrays["last"][i]),
            volume=float(arrays["volume"][i]),
            flags=int(arrays["flags"][i]),
            time_msc=int(arrays["time_msc"][i]),
        )
        for i in range(count)
    ]
