"""Cross-instrument exogenous context configuration (WO160)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ExogenousRecipe = Literal[
    "close",
    "return",
    "return_zscore",
    "rolling_corr",
    "relative_strength",
    "vol_regime",
    "direction_regime",
]

ResamplingRule = Literal["none", "last_completed"]

# v1 allowed context mappings (primary symbol, primary timeframe) -> exogenous symbols.
ALLOWED_EXOGENOUS_SYMBOLS: dict[tuple[str, str], frozenset[str]] = {
    ("WIN$", "H1"): frozenset({"WDO$"}),
    ("WDO$", "M15"): frozenset({"WIN$"}),
}


class ExogenousSeriesConfig(BaseModel):
    """One exogenous instrument aligned onto the primary evaluation frame."""

    symbol: str
    source_timeframe: str
    resampling_rule: ResamplingRule = "none"
    target_timeframe: str | None = None
    availability_lag_bars: int = Field(default=0, ge=0)
    recipes: list[ExogenousRecipe] = Field(default_factory=list)
    lookback_bars: int = Field(default=8, ge=1, le=200)
    corr_window: int = Field(default=20, ge=2, le=200)
    vol_window: int = Field(default=20, ge=2, le=200)
    vol_percentile_window: int = Field(default=60, ge=5, le=500)
    vol_regime_threshold: float = Field(default=0.60, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_resampling(self) -> ExogenousSeriesConfig:
        if self.resampling_rule == "last_completed":
            if not self.target_timeframe:
                raise ValueError(
                    "target_timeframe is required when resampling_rule is last_completed"
                )
        elif self.target_timeframe is not None:
            raise ValueError("target_timeframe is only valid with last_completed resampling")
        if not self.recipes:
            raise ValueError("recipes must contain at least one exogenous recipe")
        return self


def validate_exogenous_for_primary(
    *,
    primary_symbol: str,
    primary_timeframe: str,
    exogenous_series: list[ExogenousSeriesConfig],
) -> None:
    """Reject unsupported mappings and trading-symbol misuse."""
    allowed = ALLOWED_EXOGENOUS_SYMBOLS.get((primary_symbol, primary_timeframe.upper()))
    if not exogenous_series:
        return
    if allowed is None:
        raise ValueError(
            f"Exogenous context is not enabled for {primary_symbol} {primary_timeframe} in v1"
        )
    for spec in exogenous_series:
        if spec.symbol == primary_symbol:
            raise ValueError("Exogenous symbol must differ from the traded instrument")
        if spec.symbol not in allowed:
            raise ValueError(
                f"Exogenous symbol {spec.symbol} is not allowed for "
                f"{primary_symbol} {primary_timeframe}"
            )
        if spec.availability_lag_bars < 0:
            raise ValueError("availability_lag_bars cannot be negative")
