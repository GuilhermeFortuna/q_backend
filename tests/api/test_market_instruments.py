"""Market instruments and symbol search include local storage."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.api.main import get_market_instruments, market_data_service, search_symbols
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


def test_instruments_include_stored_symbols(market_root):
    local_store.write_ohlcv("BBAS3", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=False):
        instruments = get_market_instruments()

    symbols = {item["symbol"] for item in instruments}
    assert "PETR4" in symbols
    assert "BBAS3" in symbols

    bbas3 = next(item for item in instruments if item["symbol"] == "BBAS3")
    assert bbas3["exchange"] == "LOCAL"
    assert bbas3["assetClass"] == "equity"


def test_instruments_keep_default_metadata_for_overlapping_symbols(market_root):
    local_store.write_ohlcv("PETR4", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=False):
        instruments = get_market_instruments()

    petr4 = next(item for item in instruments if item["symbol"] == "PETR4")
    assert petr4["name"] == "PETROBRAS PN N2"
    assert petr4["exchange"] == "BOVESPA"


def test_search_finds_stored_symbols_without_mt5(market_root):
    local_store.write_ohlcv("BBAS3", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=False):
        results = search_symbols(q="bbas")

    assert len(results) == 1
    assert results[0]["symbol"] == "BBAS3"
    assert results[0]["exchange"] == "LOCAL"


def test_search_merges_local_and_mt5_hits(market_root):
    local_store.write_ohlcv("BBAS3", "D1", _bars())

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(
            market_data_service.mt5_client,
            "search_symbols",
            return_value=[
                {
                    "name": "VALE3",
                    "description": "VALE ON NM",
                    "path": "BOVESPA\\Equities\\VALE3",
                    "custom": False,
                }
            ],
        ):
            results = search_symbols(q="val")

    symbols = {item["symbol"] for item in results}
    assert "VALE3" in symbols


def test_search_returns_503_when_mt5_required_but_offline(market_root):
    set_data_source("mt5")

    with patch.object(market_data_service, "mt5_available", return_value=False):
        with pytest.raises(Exception) as exc_info:
            search_symbols(q="petr")

    assert exc_info.value.status_code == 503
