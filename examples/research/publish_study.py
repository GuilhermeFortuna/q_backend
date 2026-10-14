#!/usr/bin/env python3
"""Publish a Bollinger band and its width as a study on the Market chart.

Loads bars from the MT5 gateway, computes the band with ``research.indicators``, and
publishes it for the symbol and timeframe. Open the symbol's Market chart and enable the
study in the indicator menu. See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse

import pandas as pd

from q_backend import research
from q_backend.research import ChartIndicator
from q_backend.research import indicators


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a Bollinger band study to the Market chart.")
    parser.add_argument("--symbol", default="WDO$N", help="Symbol the study is shown for (default: WDO$N)")
    parser.add_argument("--timeframe", default="M10", help="Chart timeframe of the bars (default: M10)")
    parser.add_argument("--start", default="2026-01-01", help="First bar, in Brasília time (default: 2026-01-01)")
    parser.add_argument("--period", type=int, default=20, help="Band period in bars (default: 20)")
    parser.add_argument("--std", type=float, default=2.0, help="Band width in standard deviations (default: 2.0)")
    parser.add_argument(
        "--api-url",
        default=None,
        help="Optional override for Research API base URL (default: Q_API_URL or http://127.0.0.1:8001)",
    )
    return parser


def build_band_study(bars: pd.DataFrame, period: int, num_std: float) -> pd.DataFrame:
    """Return the band and its width on ``bars.index``."""
    upper, middle, lower = indicators.bollinger(bars["close"], period, num_std)
    study = pd.DataFrame(
        {
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
            "bb_width": upper - lower,
        },
        index=bars.index,
    )
    study["long_entry"] = bars["close"] > study["bb_upper"]
    study["score"] = bars["close"] - study["bb_middle"]
    return study


def publish_band_study(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    period: int = 20,
    num_std: float = 2.0,
    api_url: str | None = None,
    client=None,
) -> str:
    """Load the bars, compute the band and publish it. Returns the study id."""
    bars = research.load_bars(symbol, timeframe=timeframe, start=start)
    study = build_band_study(bars, period, num_std)
    return research.publish_study(
        study,
        symbol=symbol,
        name=f"Bollinger {period}/{num_std:g}",
        timeframe=timeframe,
        series=[
            ChartIndicator("bb_upper", color="#6eb5ff"),
            ChartIndicator("bb_middle", color="#6eb5ff", line_style="dotted"),
            ChartIndicator("bb_lower", color="#6eb5ff", fill_to="bb_upper"),
            ChartIndicator("long_entry", kind="markers", shape="triangle-up", position="below-bar", color="#26a69a"),
            ChartIndicator("score", kind="histogram", pane="oscillator", negative_color="#ef5350"),
        ],
        notes=f"Bollinger band over {timeframe} bars from {start}.",
        api_url=api_url,
        client=client,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    study_id = publish_band_study(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        period=args.period,
        num_std=args.std,
        api_url=args.api_url,
    )
    print(f"Published study {study_id} for {args.symbol} {args.timeframe}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
