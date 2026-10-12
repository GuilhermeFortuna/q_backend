#!/usr/bin/env python3
"""Example script publishing Matplotlib and Plotly figures to the Research stack.

See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from q_backend.research import publish_figure, use_figure_style


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish synthetic Matplotlib and Plotly figures to the Research stack."
    )
    parser.add_argument(
        "--collection",
        default="synthetic_research",
        help="Collection name to group figures under (default: synthetic_research)",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="Optional override for Research API base URL (default: Q_API_URL or http://127.0.0.1:8001)",
    )
    return parser


def create_synthetic_figures() -> tuple[Any, Any]:
    """Generate synthetic figures using Matplotlib and Plotly."""
    import matplotlib.pyplot as plt
    import plotly.graph_objects as go

    # 1. Generate synthetic equity time series
    np.random.seed(42)
    n_days = 250
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    daily_returns = np.random.normal(loc=0.0008, scale=0.015, size=n_days)
    equity = 100000.0 * np.cumprod(1.0 + daily_returns)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak

    # 2. Matplotlib figure: Equity & Drawdown
    fig, (ax_eq, ax_dd) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("Synthetic Strategy Equity & Drawdown")

    ax_eq.plot(dates, equity, label="Realized Equity", color="#ffca47")
    ax_eq.set_ylabel("Equity (BRL)")
    ax_eq.legend(loc="upper left")

    ax_dd.fill_between(dates, drawdown * 100, 0, color="#fb7185", alpha=0.5, label="Drawdown %")
    ax_dd.set_ylabel("Drawdown %")
    ax_dd.set_xlabel("Date")
    ax_dd.legend(loc="lower left")

    # 3. Plotly figure: Interactive Return Distribution & Trace
    p_fig = go.Figure()
    p_fig.add_trace(
        go.Scatter(
            x=dates,
            y=daily_returns * 100,
            mode="lines+markers",
            name="Daily Return %",
            marker=dict(size=4),
        )
    )
    p_fig.update_layout(
        title=dict(text="Synthetic Daily Returns Time Series"),
        xaxis_title="Date",
        yaxis_title="Return (%)",
    )

    return fig, p_fig


def publish_synthetic_figures(
    *,
    collection: str = "synthetic_research",
    api_url: str | None = None,
    client: Any = None,
) -> tuple[str, str]:
    """Apply styling, create synthetic figures, and publish both to the stack."""
    import matplotlib.pyplot as plt

    use_figure_style("dark")

    mpl_fig, plotly_fig = create_synthetic_figures()

    try:
        mpl_id = publish_figure(
            mpl_fig,
            collection=collection,
            notes="Synthetic equity curve and drawdown (Matplotlib PNG)",
            image_format="png",
            api_url=api_url,
            client=client,
        )

        plotly_id = publish_figure(
            plotly_fig,
            collection=collection,
            notes="Interactive daily returns trace (Plotly JSON)",
            api_url=api_url,
            client=client,
        )
    finally:
        plt.close(mpl_fig)

    return mpl_id, plotly_id


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mpl_id, plotly_id = publish_synthetic_figures(
        collection=args.collection,
        api_url=args.api_url,
    )
    print(f"Published Matplotlib figure: {mpl_id}")
    print(f"Published Plotly figure: {plotly_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
