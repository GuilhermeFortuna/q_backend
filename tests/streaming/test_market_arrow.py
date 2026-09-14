import numpy as np
import pyarrow as pa

from q_backend.streaming.market.arrow import BARS_SCHEMA, TICKS_SCHEMA, bars_to_ipc, ticks_to_ipc


def test_tick_ipc_uses_contract_schema():
    payload = ticks_to_ipc(
        {
            "time_msc": np.array([1, 2, 3]),
            "bid": np.ones(3),
            "ask": np.ones(3),
            "last": np.ones(3),
            "volume": np.ones(3),
            "flags": np.ones(3, dtype=np.int32),
        }
    )
    assert pa.ipc.open_stream(payload).schema == TICKS_SCHEMA


def test_bar_ipc_uses_contract_schema():
    payload = bars_to_ipc(
        {
            "time": np.array([1]),
            "open": np.ones(1),
            "high": np.ones(1),
            "low": np.ones(1),
            "close": np.ones(1),
            "tick_volume": np.ones(1, dtype=np.int64),
            "spread": np.zeros(1, dtype=np.int64),
            "real_volume": np.zeros(1, dtype=np.int64),
        }
    )
    assert pa.ipc.open_stream(payload).schema == BARS_SCHEMA
