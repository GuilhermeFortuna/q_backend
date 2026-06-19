"""OHLCV endpoints route stored symbols through the local parquet provider."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from q_backend.api.dependencies import market_data_service
from q_backend.api.routers.market import get_market_ohlcv_available_range
from q_backend.market_data import api_service as market_service
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.storage.runtime_config import set_data_source


def _bars() -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime(2024, 3, 1),
            open=40.0,
            high=41.0,
            low=39.0,
            close=40.5,
            tick_volume=1000,
        ),
        OHLCV(
            time=datetime(2024, 3, 2),
            open=40.5,
            high=41.5,
            low=40.0,
            close=41.0,
            tick_volume=1100,
        ),
    ]


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


def test_ohlcv_available_range_uses_local_store_when_symbol_not_in_mt5(market_root):
    local_store.write_ohlcv("BGI$", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            return_value=False,
        ):
            available = market_service.fetch_ohlcv_available_range(
                market_data_service, "BGI$", "D1"
            )

    assert available is not None
    assert available.symbol == "BGI$"
    assert available.bar_count == 2


def test_ohlcv_count_query_reads_local_store_when_symbol_not_in_mt5(market_root):
    local_store.write_ohlcv("BGI$", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            return_value=False,
        ):
            rows = market_service.fetch_ohlcv_rows(
                market_data_service, "BGI$", "D1", count=500, start=None, end=None
            )

    assert len(rows) == 2
    assert rows[-1].close == 41.0


def test_market_ohlcv_response_shape_for_local_bars(market_root):
    local_store.write_ohlcv("BGI$", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            return_value=False,
        ):
            rows = market_service.fetch_ohlcv_rows(
                market_data_service, "BGI$", "D1", count=500, start=None, end=None
            )

    payload = [market_service.ohlcv_to_bar_response(row) for row in rows]
    assert len(payload) == 2
    assert payload[0]["close"] == 40.5


def test_market_ohlcv_available_range_endpoint_returns_local_bounds(market_root):
    local_store.write_ohlcv("BGI$", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            return_value=False,
        ):
            payload = get_market_ohlcv_available_range("BGI$", timeframe="D1", mds=market_data_service)

    assert payload["symbol"] == "BGI$"
    assert payload["bar_count"] == 2


def test_ohlcv_uses_mt5_when_symbol_exists_in_terminal(market_root):
    mock_rates = MagicMock()
    mock_rates.__len__.return_value = 1
    mock_rates.__iter__.return_value = iter(
        [
            {
                "time": 1_709_251_200,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "tick_volume": 10,
                "spread": 1,
                "real_volume": 0,
            }
        ]
    )

    mock_mt5 = MagicMock()
    mock_mt5.symbol_select.return_value = True
    mock_mt5.copy_rates_from_pos.return_value = mock_rates

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.market_data.routing.symbol_selectable_in_mt5",
            return_value=True,
        ):
            with patch("q_backend.market_data.clients.metatrader.mt5", mock_mt5):
                rows = market_service.fetch_ohlcv_rows(
                    market_data_service, "PETR4", "D1", count=1, start=None, end=None
                )

    assert len(rows) == 1
    mock_mt5.copy_rates_from_pos.assert_called_once()
