"""OHLCV routing when the MT5 package is present but the terminal is offline."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.market_data.routing import resolve_ohlcv_source
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import set_data_source


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def reset_data_source():
    set_data_source("auto")
    yield
    set_data_source("auto")


def test_auto_mode_uses_local_when_mt5_offline(market_root):
    local_store.write_ohlcv(
        "CCM$",
        "H1",
        [
            OHLCV(
                time=datetime(2024, 1, 1, 10),
                open=1,
                high=1,
                low=1,
                close=1,
                tick_volume=1,
            )
        ],
    )
    service = MarketDataService()

    with patch.object(service, "mt5_available", return_value=False):
        assert resolve_ohlcv_source(service, "CCM$", "H1") == "local"
        bars = service.get_ohlcv(
            "CCM$",
            "H1",
            datetime(2024, 1, 1),
            datetime(2024, 1, 2),
        )

    assert len(bars) == 1
