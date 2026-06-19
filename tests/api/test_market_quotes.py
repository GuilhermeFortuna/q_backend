import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from q_backend.api.main import (
    _build_market_snapshot,
    _format_tape_ticks,
    _symbol_info_to_instrument_response,
    _tick_side,
    _utc_iso_milliseconds,
    get_market_instrument_info,
    get_market_snapshot,
    get_market_snapshots,
    get_market_ticks,
    market_data_service,
)
from q_backend.storage.runtime_config import set_data_source
from q_backend.market_data.models import Tick
from q_backend.market_data.timezone import unix_seconds_to_utc_iso

_RATE_DTYPE = [
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
]

_TICK_TIME = 1_749_486_731


def _d1_rates(prev_close: float = 41.22, today_close: float = 41.08) -> np.ndarray:
    return np.array(
        [
            (
                1_749_400_800,
                prev_close,
                prev_close + 0.3,
                prev_close - 0.2,
                prev_close,
                1_000,
                1,
                0,
            ),
            (
                1_749_487_200,
                41.2,
                41.55,
                40.9,
                today_close,
                2_000,
                1,
                0,
            ),
        ],
        dtype=_RATE_DTYPE,
    )


@pytest.fixture
def mock_mt5():
    mock = MagicMock()
    mock.TIMEFRAME_M1 = 1
    mock.TIMEFRAME_D1 = 16408
    mock.TICK_FLAG_BUY = 32
    mock.TICK_FLAG_SELL = 64
    mock.COPY_TICKS_ALL = 7
    with patch.dict(sys.modules, {"MetaTrader5": mock}):
        with patch("q_backend.api.main.mt5_client_module.mt5", mock):
            yield mock


def test_build_market_snapshot_enriched_fields(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(digits=2)
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(
        bid=41.07,
        ask=41.09,
        last=41.08,
        volume=45_683_500,
        time=_TICK_TIME,
    )
    mock_mt5.copy_rates_from_pos.side_effect = lambda symbol, tf, pos, count: (
        _d1_rates() if tf == mock_mt5.TIMEFRAME_D1 else None
    )

    snapshot = _build_market_snapshot("PETR4")

    assert snapshot is not None
    assert snapshot["symbol"] == "PETR4"
    assert snapshot["last"] == 41.08
    assert snapshot["changePct"] == pytest.approx(-0.3396448, rel=1e-4)
    assert snapshot["volume"] == 45_683_500
    assert snapshot["bid"] == 41.07
    assert snapshot["ask"] == 41.09
    assert snapshot["spread"] == pytest.approx(0.02)
    assert snapshot["changeAbs"] == pytest.approx(-0.14)
    assert snapshot["dayOpen"] == 41.2
    assert snapshot["dayHigh"] == 41.55
    assert snapshot["dayLow"] == 40.9
    assert snapshot["prevClose"] == 41.22
    assert snapshot["digits"] == 2
    assert snapshot["tickTime"] == unix_seconds_to_utc_iso(_TICK_TIME)


def test_build_market_snapshot_market_closed_fallback(mock_mt5):
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(digits=2)
    mock_mt5.symbol_info_tick.return_value = None
    mock_mt5.copy_rates_from_pos.side_effect = lambda symbol, tf, pos, count: (
        _d1_rates(today_close=41.0)
        if tf == mock_mt5.TIMEFRAME_D1
        else np.array(
            [(1_749_487_100, 41.0, 41.0, 41.0, 41.0, 500, 1, 0)],
            dtype=_RATE_DTYPE,
        )
    )

    snapshot = _build_market_snapshot("PETR4")

    assert snapshot is not None
    assert snapshot["last"] == 41.0
    assert snapshot["bid"] == 41.0
    assert snapshot["ask"] == 41.0
    assert snapshot["spread"] == 0.0
    assert snapshot["tickTime"] is None


def test_get_market_snapshot_returns_503_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json")
    )
    set_data_source("mt5")
    with patch.object(market_data_service, "mt5_available", return_value=False):
        with pytest.raises(Exception) as exc_info:
            get_market_snapshot("PETR4")
    assert exc_info.value.status_code == 503


def test_get_market_snapshots_returns_multiple_and_skips_bad_symbol(mock_mt5):
    good_snapshot = {
        "symbol": "PETR4",
        "last": 41.08,
        "changePct": -0.34,
        "volume": 100,
        "bid": 41.07,
        "ask": 41.09,
        "spread": 0.02,
        "changeAbs": -0.14,
        "dayOpen": 41.2,
        "dayHigh": 41.55,
        "dayLow": 40.9,
        "prevClose": 41.22,
        "digits": 2,
        "tickTime": "2026-06-09T14:32:11Z",
    }

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch(
            "q_backend.api.main._build_market_snapshot",
            side_effect=lambda symbol: good_snapshot if symbol == "PETR4" else None,
        ):
            body = get_market_snapshots(symbols="PETR4,BADSYM,VALE3")

    assert len(body["snapshots"]) == 1
    assert body["snapshots"][0]["symbol"] == "PETR4"


def test_get_market_snapshots_rejects_more_than_50_symbols():
    symbols = ",".join(f"SYM{i}" for i in range(51))
    with pytest.raises(Exception) as exc_info:
        get_market_snapshots(symbols=symbols)
    assert exc_info.value.status_code == 422


def test_get_market_snapshots_returns_503_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json")
    )
    set_data_source("mt5")
    with patch.object(market_data_service, "mt5_available", return_value=False):
        with pytest.raises(Exception) as exc_info:
            get_market_snapshots(symbols="PETR4")
    assert exc_info.value.status_code == 503


def test_tick_side_mapping(mock_mt5):
    assert _tick_side(32) == "buy"
    assert _tick_side(64) == "sell"
    assert _tick_side(96) is None
    assert _tick_side(0) is None


def test_format_tape_ticks_filters_informational_ticks(mock_mt5):
    raw_ticks = [
        Tick(
            time=datetime(2026, 6, 9, 14, 32, 11),
            bid=41.07,
            ask=41.09,
            last=41.08,
            volume=300,
            flags=32,
            time_msc=1_749_486_731_123,
        ),
        Tick(
            time=datetime(2026, 6, 9, 14, 32, 12),
            bid=41.08,
            ask=41.10,
            last=0.0,
            volume=0.0,
            flags=0,
            time_msc=1_749_486_732_000,
        ),
    ]

    formatted = _format_tape_ticks(raw_ticks)

    assert len(formatted) == 1
    assert formatted[0]["side"] == "buy"
    assert formatted[0]["last"] == 41.08
    assert formatted[0]["timestamp"] == _utc_iso_milliseconds(1_749_486_731_123)


def test_format_tape_ticks_falls_back_to_quote_ticks_for_fx(mock_mt5):
    raw_ticks = [
        Tick(
            time=datetime(2026, 6, 9, 14, 32, 11),
            bid=1.08450,
            ask=1.08470,
            last=0.0,
            volume=0.0,
            flags=0,
            time_msc=1_749_486_731_500,
        )
    ]

    formatted = _format_tape_ticks(raw_ticks)

    assert len(formatted) == 1
    assert formatted[0]["side"] is None
    assert formatted[0]["last"] == 0.0


def test_get_market_ticks_respects_limit_and_returns_newest_last(mock_mt5):
    ticks = [
        Tick(
            time=datetime(2026, 6, 9, 14, 32, 10),
            bid=41.06,
            ask=41.08,
            last=41.07,
            volume=100,
            flags=32,
            time_msc=1_749_486_730_000,
        ),
        Tick(
            time=datetime(2026, 6, 9, 14, 32, 11),
            bid=41.07,
            ask=41.09,
            last=41.08,
            volume=300,
            flags=64,
            time_msc=1_749_486_731_000,
        ),
    ]

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(
            market_data_service, "get_recent_ticks", return_value=ticks
        ) as get_recent:
            body = get_market_ticks("PETR4", limit=1)

    get_recent.assert_called_once_with("PETR4", 1)
    assert len(body["ticks"]) == 1
    assert body["ticks"][0]["side"] == "sell"
    assert body["ticks"][0]["timestamp"].endswith("Z")


def test_get_market_ticks_returns_404_for_unknown_symbol():
    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(market_data_service, "get_recent_ticks", return_value=[]):
            with patch.object(
                market_data_service, "get_symbol_info", return_value=None
            ):
                with pytest.raises(Exception) as exc_info:
                    get_market_ticks("UNKNOWN")
    assert exc_info.value.status_code == 404


def test_symbol_info_to_instrument_response_maps_fields():
    info = {
        "description": "Mini Indice Bovespa",
        "path": "BMF\\Futures\\WIN$",
        "currency_base": "BRL",
        "currency_profit": "BRL",
        "digits": 0,
        "point": 1.0,
        "trade_tick_size": 5.0,
        "trade_tick_value": 1.0,
        "trade_contract_size": 0.2,
        "volume_min": 1.0,
        "volume_max": 500.0,
        "volume_step": 1.0,
        "spread_float": True,
    }

    body = _symbol_info_to_instrument_response("WIN$", info)

    assert body == {
        "symbol": "WIN$",
        "description": "Mini Indice Bovespa",
        "exchange": "BMF",
        "currencyBase": "BRL",
        "currencyProfit": "BRL",
        "digits": 0,
        "point": 1.0,
        "tickSize": 5.0,
        "tickValue": 1.0,
        "contractSize": 0.2,
        "volumeMin": 1.0,
        "volumeMax": 500.0,
        "volumeStep": 1.0,
        "spreadFloating": True,
    }


def test_get_market_instrument_info_returns_404_for_unknown_symbol():
    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(market_data_service, "get_symbol_info", return_value=None):
            with pytest.raises(Exception) as exc_info:
                get_market_instrument_info("UNKNOWN")
    assert exc_info.value.status_code == 404
