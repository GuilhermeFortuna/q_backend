"""Exercise the research load example without a live gateway."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from examples.research.load_market_data import build_parser, main
from q_backend.market_data.timezone import BRASILIA_TZ


def test_load_market_data_help_documents_unadjusted_symbol() -> None:
    help_text = build_parser().format_help()
    assert "WIN$N" in help_text


def test_example_main_calls_load_bars_with_optional_end() -> None:
    idx = pd.DatetimeIndex(["2024-12-16T10:00:00"], tz=BRASILIA_TZ, name="time")
    frame = pd.DataFrame(
        {
            "open": [35.0],
            "high": [36.0],
            "low": [34.5],
            "close": [35.5],
            "tick_volume": [10000],
            "spread": [1.0],
            "real_volume": [500000.0],
        },
        index=idx,
    )
    frame.attrs["q_research"] = {"source": "mt5"}

    calls: list[dict] = []

    def fake_load_bars(symbol, **kwargs):
        calls.append({"symbol": symbol, **kwargs})
        return frame

    with patch("examples.research.load_market_data.load_bars", fake_load_bars):
        code = main(
            [
                "--symbol",
                "PETR4",
                "--timeframe",
                "D1",
                "--start",
                "2024-12-16",
                "--gateway-url",
                "http://gw.test",
            ]
        )
    assert code == 0
    assert calls == [
        {
            "symbol": "PETR4",
            "timeframe": "D1",
            "start": "2024-12-16",
            "end": None,
            "gateway_url": "http://gw.test",
            "gateway_token": None,
        }
    ]
