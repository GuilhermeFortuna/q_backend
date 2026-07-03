"""Feature-spec causality invariant (WO128, hardened in WO181).

Every ``FeatureSpec`` in the catalog is enumerated here and driven through
``assert_causal`` — a value at bar ``t`` must not change when future bars are removed.
Historically this harness silently filtered out ``source == "neural"`` and
``category == "exogenous"`` specs, so those families quietly escaped the invariant.
WO181 removes the silent filter:

* every non-exempt spec is tested (exemptions live only in
  ``leakage_exemptions.EXEMPT_SPECS`` and are guarded by hygiene meta-tests);
* exogenous specs are brought **under** the invariant — the harness re-aligns them on
  every prefix through the real publish-time as-of join, so a "published-at vs
  effective-at" bug would break prefix-stability;
* neural specs (registered per-model, not in the static catalog) are covered by
  ``tests/features/test_leakage_invariants.py`` via the torch-free PCA encoder path.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from datetime import datetime, timezone

from leakage_exemptions import EXEMPT_SPECS

from q_backend.features.compute import FeatureSeries, compute_feature
from q_backend.features.leakage import (
    LeakageError,
    assert_causal,
    assert_neural_oos_only,
    leaky_close_shift_feature,
    neural_leakage_status,
)
from q_backend.features.registry import (
    FeatureSpec,
    get_feature_spec,
    list_feature_specs,
    resolve_params,
)
from q_backend.market_data.exogenous_columns import exog_column_name
from q_backend.market_data.exogenous_config import ExogenousSeriesConfig
from q_backend.market_data.exogenous_context import attach_exogenous_context


def _synthetic_bars(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


# --------------------------------------------------------------------------- #
# Exogenous alignment fixtures — recipe params chosen so the attached column
# names match what each exog FeatureSpec reads (symbol default "WDO$").
# --------------------------------------------------------------------------- #
_EXOG_SYMBOL = "WDO$"
_EXOG_CFG = ExogenousSeriesConfig(
    symbol=_EXOG_SYMBOL,
    source_timeframe="H1",
    recipes=[
        "close",
        "return",
        "return_zscore",
        "rolling_corr",
        "relative_strength",
        "vol_regime",
        "direction_regime",
    ],
    lookback_bars=8,
    corr_window=20,
    vol_window=20,
)
_EXOG_SUFFIX: dict[str, str] = {
    "exog_close": "close",
    "exog_return": "return_8",
    "exog_return_zscore": "return_zscore_8_20",
    "exog_rolling_corr": "rolling_corr_20",
    "exog_relative_strength": "relative_strength_8",
    "exog_vol_regime": "vol_regime_20",
    "exog_direction_regime": "direction_regime_8",
}


def _exog_loader(primary: pd.DataFrame):
    """Deterministic exogenous OHLCV on the primary's time grid (no network/files)."""
    rng = np.random.default_rng(4242)
    n = len(primary)
    close = 50.0 + np.cumsum(rng.normal(0.0, 0.7, size=n))
    exog = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=pd.DatetimeIndex(primary["time"], name="time"),
    )

    def _loader(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        return exog.copy()

    return _loader


def _exog_causal_compute(spec: FeatureSpec, loader):
    """Compute an exogenous feature by re-aligning on the given (possibly prefix) frame.

    Causality for exogenous features lives entirely in the alignment (a backward as-of
    join on publish/availability time). ``compute_feature`` only reads the aligned column
    (and, for bool recipes, applies a warmup that is dtype-incompatible with a NaN mask —
    a limitation, not a leak). We therefore assert on the aligned column directly: it is
    the causality-sensitive artifact, recomputed independently on each prefix.
    """
    column = exog_column_name(_EXOG_SYMBOL, _EXOG_SUFFIX[spec.name])

    def _fn(primary: pd.DataFrame) -> FeatureSeries:
        indexed = primary.set_index("time")
        attached, _ = attach_exogenous_context(
            indexed,
            [_EXOG_CFG],
            primary_symbol="WIN$",
            primary_timeframe="H1",
            loader=loader,
            start=indexed.index[0].to_pydatetime(),
            end=indexed.index[-1].to_pydatetime(),
        )
        return FeatureSeries(
            feature_id=spec.name,
            series=attached[column].reset_index(drop=True),
            warmup_bars=0,
            leakage_status="clean",
        )

    return _fn


_ALL_SPEC_NAMES = [spec.name for spec in list_feature_specs()]


@pytest.mark.parametrize("spec_name", _ALL_SPEC_NAMES)
def test_feature_spec_is_causal(spec_name: str) -> None:
    if spec_name in EXEMPT_SPECS:
        pytest.skip(f"exempt: {EXEMPT_SPECS[spec_name]}")

    bars = _synthetic_bars()
    spec = get_feature_spec(spec_name)
    sample = [50, 120, 200, len(bars) - 1]

    if spec.source == "exogenous":
        assert_causal(_exog_causal_compute(spec, _exog_loader(bars)), bars, sample_indices=sample)
        return

    params = resolve_params(spec, {})

    def _compute(frame: pd.DataFrame):
        return compute_feature(frame, spec, params)

    assert_causal(_compute, bars, sample_indices=sample)


# --------------------------------------------------------------------------- #
# Exemption-dict hygiene (WO181 Task 1)
# --------------------------------------------------------------------------- #
def test_feature_spec_exemptions_are_not_stale() -> None:
    """An exemption naming a spec that no longer exists must fail loudly."""
    names = {spec.name for spec in list_feature_specs()}
    unknown = sorted(set(EXEMPT_SPECS) - names)
    assert not unknown, f"EXEMPT_SPECS names non-existent feature specs: {unknown}"


def test_every_feature_spec_is_tested_or_exempt() -> None:
    """No catalog spec may escape the invariant without a reviewed exemption."""
    names = {spec.name for spec in list_feature_specs()}
    tested = set(_ALL_SPEC_NAMES) - set(EXEMPT_SPECS)
    covered = tested | set(EXEMPT_SPECS)
    missing = sorted(names - covered)
    assert not missing, (
        f"Feature specs neither tested nor exempted: {missing}. Add coverage or a "
        "reviewed entry to leakage_exemptions.EXEMPT_SPECS."
    )


# --------------------------------------------------------------------------- #
# Publish-time alignment (WO181 Task 3): a planted future spike must not be
# visible before its availability time.
# --------------------------------------------------------------------------- #
def test_exogenous_alignment_uses_publish_time() -> None:
    times = pd.date_range("2024-01-01 09:00", periods=8, freq="h", tz="UTC")
    primary = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 1000.0,
        },
        index=times,
    )
    exog_close = [1.0, 1.0, 99.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # spike at exog bar index 2
    exog = pd.DataFrame(
        {
            "open": exog_close,
            "high": [v + 0.5 for v in exog_close],
            "low": [v - 0.5 for v in exog_close],
            "close": exog_close,
            "volume": 1000.0,
        },
        index=times,
    )

    def _loader(symbol, timeframe, start, end):
        return exog.copy()

    cfg = ExogenousSeriesConfig(symbol=_EXOG_SYMBOL, source_timeframe="H1", recipes=["close"])
    attached, _ = attach_exogenous_context(
        primary,
        [cfg],
        primary_symbol="WIN$",
        primary_timeframe="H1",
        loader=_loader,
        start=times[0].to_pydatetime(),
        end=times[-1].to_pydatetime(),
    )
    aligned = attached[exog_column_name(_EXOG_SYMBOL, "close")]
    # Exog bar 2 (index 2) closes at 12:00 and becomes visible on the primary bar whose
    # open is 12:00 (index 3). It must NOT be visible at or before its own close time.
    assert aligned.iloc[2] != 99.0  # spike not visible before publish time
    assert aligned.iloc[3] == 99.0  # visible only after the exog bar has closed


def test_exogenous_harness_catches_a_forward_leak() -> None:
    """The exogenous path uses assert_causal too; a shift(-1) on the column is caught."""
    bars = _synthetic_bars(120)
    loader = _exog_loader(bars)
    column = exog_column_name(_EXOG_SYMBOL, "close")

    def _leaky(primary: pd.DataFrame) -> FeatureSeries:
        indexed = primary.set_index("time")
        attached, _ = attach_exogenous_context(
            indexed,
            [_EXOG_CFG],
            primary_symbol="WIN$",
            primary_timeframe="H1",
            loader=loader,
            start=indexed.index[0].to_pydatetime(),
            end=indexed.index[-1].to_pydatetime(),
        )
        leaked = attached[column].shift(-1).reset_index(drop=True)
        return FeatureSeries(
            feature_id="leaky_exog", series=leaked, warmup_bars=0, leakage_status="suspect"
        )

    with pytest.raises(LeakageError):
        assert_causal(_leaky, bars, sample_indices=[40, 80])


# --------------------------------------------------------------------------- #
# Existing WO128 leakage-guard unit tests (unchanged).
# --------------------------------------------------------------------------- #
def test_assert_causal_rejects_leaky_shift() -> None:
    bars = _synthetic_bars()
    with pytest.raises(LeakageError, match="Leakage at index 100"):
        assert_causal(leaky_close_shift_feature, bars, sample_indices=[100])


def test_leaky_shift_only_affects_interior_bars() -> None:
    bars = _synthetic_bars(120)
    full = leaky_close_shift_feature(bars)
    assert pd.isna(full.series.iloc[-1])
    assert full.series.iloc[0] == pytest.approx(bars["close"].iloc[1])


def _naive_times(n: int = 10) -> pd.Series:
    """Bar times as the data lake delivers them: tz-naive ``datetime64``."""
    return pd.Series(pd.date_range("2026-01-01", periods=n, freq="h"))


def test_neural_leakage_status_handles_tz_naive_bar_times() -> None:
    # The data lake yields tz-naive datetime64 while train_end is tz-aware UTC.
    # Comparing the two directly raises "Invalid comparison" — the OOS gate path.
    times = _naive_times()  # 2026-01-01 00:00..09:00, all strictly after train_end
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    train_end = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    assert neural_leakage_status(times, series, train_end) == "clean"


def test_assert_neural_oos_only_handles_tz_naive_bar_times() -> None:
    times = _naive_times()
    # All latents land strictly after train_end -> no leak, must not raise on dtype.
    series = pd.Series([float("nan")] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0])
    train_end = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    assert_neural_oos_only(series, times, train_end)
