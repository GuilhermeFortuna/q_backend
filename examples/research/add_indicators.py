#!/usr/bin/env python3
"""Offline example script adding RSI, EMA, and ATR indicators to a Parquet frame.

See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd

from q_backend.research import indicators


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline indicator enrichment reading and writing a Parquet file.")
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to input Parquet file with OHLC bars",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path to write enriched Parquet file",
    )
    return parser


def add_indicators_to_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Enrich a bars DataFrame with RSI, EMA, and ATR columns."""
    df = frame.copy()
    df["rsi"] = indicators.rsi(df["close"], period=14)
    df["ema_21"] = indicators.ma(df["close"], period=21, kind="ema")
    df["atr"] = indicators.atr(df, period=14)
    return df


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path: Path = args.input
    output_path: Path = args.output

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    frame = pd.read_parquet(input_path)
    enriched = add_indicators_to_frame(frame)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_parquet(output_path)

    print(f"Enriched {len(enriched)} rows with columns {list(enriched.columns)} -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
