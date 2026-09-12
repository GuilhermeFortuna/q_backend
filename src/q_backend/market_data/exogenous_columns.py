"""Namespaced exogenous column helpers (WO160)."""

from __future__ import annotations

import pandas as pd


def symbol_namespace(symbol: str) -> str:
    return symbol.replace("$", "").replace("/", "_").replace("\\", "_")


def exog_column_name(symbol: str, suffix: str) -> str:
    return f"exog_{symbol_namespace(symbol)}_{suffix}"


def resolve_exog_column(df: pd.DataFrame, symbol: str, suffix: str) -> pd.Series:
    column = exog_column_name(symbol, suffix)
    if column not in df.columns:
        raise KeyError(
            f"Missing exogenous column '{column}'. " "Ensure exogenous_series is configured for this research run."
        )
    return df[column]
