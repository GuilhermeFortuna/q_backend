"""Exercise the research load example without live catalog or gateway."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from examples.research.load_market_data import main
from q_backend.market_data.timezone import BRASILIA_TZ


def test_example_main_uses_research_with_cli_args() -> None:
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
    frame.attrs["q_research"] = {"source": "local", "dataset_id": "test"}

    class FakeResearch:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def bars(self, symbol, *, timeframe, start, end):
            assert symbol == "PETR4"
            assert timeframe == "D1"
            assert start == "2024-12-16"
            return frame

    with patch("examples.research.load_market_data.Research", FakeResearch):
        code = main(
            [
                "--source",
                "local",
                "--database-url",
                "sqlite:///:memory:",
                "--market-data-root",
                "/tmp/q-market",
                "--symbol",
                "PETR4",
                "--timeframe",
                "D1",
                "--start",
                "2024-12-16",
                "--end",
                "2024-12-17",
            ]
        )
    assert code == 0
