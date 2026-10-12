"""Q dark theme for Matplotlib and Plotly figures."""

from __future__ import annotations

# Tokens copied from q_frontend (src/styles/globals.css, src/lib/charts/chartTheme.ts)
BG_CARBON_950 = "#0a0b0c"
PLOT_CARBON_900 = "#111315"
EDGE_CARBON_700 = "#22262c"
GRID_CARBON_800 = "#181b1f"
TEXT_SILVER_100 = "#e8eaed"
SUBTEXT_SILVER_200 = "#c5c9d0"
MUTED_SILVER_400 = "#6f7785"

SERIES_PALETTE = [
    "#ffca47",  # gold-400
    "#d99e22",  # brass-500
    "#c9b896",  # cream-300
    "#9aa1ac",  # silver-300
    "#6f7785",  # silver-400
    "#b8831c",  # brass-600
    "#34d399",  # emerald-400 (positive)
    "#fb7185",  # rose-400 (negative)
]


def use_figure_style(theme: str = "dark") -> None:
    """Apply the Q dark theme to installed plotting libraries (Matplotlib, Plotly)."""
    if theme != "dark":
        raise ValueError(f"Unsupported theme {theme!r}. Only 'dark' is supported.")
    # 1. Matplotlib rcParams
    try:
        import matplotlib as mpl

        mpl.rcParams["figure.facecolor"] = BG_CARBON_950
        mpl.rcParams["axes.facecolor"] = PLOT_CARBON_900
        mpl.rcParams["axes.edgecolor"] = EDGE_CARBON_700
        mpl.rcParams["axes.labelcolor"] = SUBTEXT_SILVER_200
        mpl.rcParams["xtick.color"] = MUTED_SILVER_400
        mpl.rcParams["ytick.color"] = MUTED_SILVER_400
        mpl.rcParams["text.color"] = TEXT_SILVER_100
        mpl.rcParams["grid.color"] = GRID_CARBON_800
        mpl.rcParams["grid.alpha"] = 0.5
        mpl.rcParams["axes.prop_cycle"] = mpl.cycler(color=SERIES_PALETTE)
    except ImportError:
        pass

    # 2. Plotly template
    try:
        import plotly.graph_objects as go
        import plotly.io as pio

        q_dark = go.layout.Template(
            layout=go.Layout(
                paper_bgcolor=BG_CARBON_950,
                plot_bgcolor=PLOT_CARBON_900,
                font=dict(color=TEXT_SILVER_100),
                colorway=SERIES_PALETTE,
                xaxis=dict(
                    gridcolor=GRID_CARBON_800,
                    linecolor=EDGE_CARBON_700,
                    tickfont=dict(color=MUTED_SILVER_400),
                    title=dict(font=dict(color=SUBTEXT_SILVER_200)),
                ),
                yaxis=dict(
                    gridcolor=GRID_CARBON_800,
                    linecolor=EDGE_CARBON_700,
                    tickfont=dict(color=MUTED_SILVER_400),
                    title=dict(font=dict(color=SUBTEXT_SILVER_200)),
                ),
            )
        )
        pio.templates["q_dark"] = q_dark
        pio.templates.default = "q_dark"
    except ImportError:
        pass
