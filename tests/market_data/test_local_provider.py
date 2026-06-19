"""LocalParquetClient reads from the local store (WO48)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from q_backend.market_data.clients.local import LocalParquetClient
from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.market_data.models import OHLCV


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def test_local_get_ohlcv_matches_written_bars(market_root):
    bars = [
        OHLCV(
            time=datetime(2024, 6, 1),
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            tick_volume=500,
            spread=1,
            real_volume=0,
        ),
        OHLCV(
            time=datetime(2024, 6, 2),
            open=10.5,
            high=11.5,
            low=10.0,
            close=11.0,
            tick_volume=600,
            spread=1,
            real_volume=0,
        ),
    ]
    local_store.write_ohlcv("PETR4", "D1", bars)

    client = LocalParquetClient()
    result = client.get_ohlcv(
        "PETR4", "D1", datetime(2024, 6, 1), datetime(2024, 6, 2)
    )

    assert len(result) == 2
    assert result[0].model_dump() == bars[0].model_dump()
    assert result[1].model_dump() == bars[1].model_dump()


def test_local_search_and_symbol_info(market_root):
    local_store.write_ohlcv(
        "PETR4",
        "D1",
        [
            OHLCV(
                time=datetime(2024, 1, 1),
                open=1,
                high=1,
                low=1,
                close=1,
                tick_volume=1,
            )
        ],
    )
    client = LocalParquetClient()
    matches = client.search_symbols("pet")
    assert len(matches) == 1
    assert matches[0]["name"] == "PETR4"

    info = client.get_symbol_info("PETR4")
    assert info is not None
    assert info["name"] == "PETR4"
