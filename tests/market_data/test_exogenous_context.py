"""Tests for causal cross-instrument exogenous context (WO160)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.genome.schema import Genome
from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec
from q_backend.market_data.exogenous_columns import exog_column_name
from q_backend.market_data.exogenous_context import (
    align_exogenous_close,
    attach_exogenous_context,
    clear_evaluation_frame_cache,
    prepare_evaluation_frame,
    resample_completed_bars,
)
from q_backend.market_data.exogenous_config import (
    ExogenousSeriesConfig,
    validate_exogenous_for_primary,
)
from q_backend.optimization.strategy_search import StrategySearchConfig


def _ohlcv_frame(index: pd.DatetimeIndex, close_values: list[float]) -> pd.DataFrame:
    close = pd.Series(close_values, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def _loader_from_frames(frames: dict[tuple[str, str], pd.DataFrame]):
    calls: list[tuple[str, str, datetime, datetime]] = []

    def _loader(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        calls.append((symbol, timeframe, start, end))
        return frames[(symbol, timeframe)].copy()

    _loader.calls = calls  # type: ignore[attr-defined]
    return _loader


class TestExogenousAlignment:
    def setup_method(self) -> None:
        clear_evaluation_frame_cache()

    def test_planted_spike_visible_only_after_lag(self) -> None:
        primary_index = pd.date_range("2024-01-01 09:00", periods=6, freq="1h")
        primary = _ohlcv_frame(primary_index, [100, 100, 100, 100, 100, 100])

        exog_index = pd.date_range("2024-01-01 09:00", periods=6, freq="1h")
        exog_values = [1.0, 1.0, 100.0, 1.0, 1.0, 1.0]
        exog = _ohlcv_frame(exog_index, exog_values)

        aligned_no_lag = align_exogenous_close(
            primary.index,
            exog,
            aligned_timeframe="H1",
            primary_timeframe="H1",
            availability_lag_bars=0,
        )
        # Exogenous H1 values become visible at bar close; primary bar i aligns to prior closes.
        assert aligned_no_lag.iloc[3] == 100.0

        aligned_lag = align_exogenous_close(
            primary.index,
            exog,
            aligned_timeframe="H1",
            primary_timeframe="H1",
            availability_lag_bars=1,
        )
        assert aligned_lag.iloc[3] != 100.0
        assert aligned_lag.iloc[4] == 100.0

    def test_negative_lag_rejected(self) -> None:
        primary_index = pd.date_range("2024-01-01", periods=3, freq="1h")
        exog = _ohlcv_frame(primary_index, [1.0, 2.0, 3.0])
        with pytest.raises(ValueError, match="cannot be negative"):
            align_exogenous_close(
                primary_index,
                exog,
                aligned_timeframe="H1",
                primary_timeframe="H1",
                availability_lag_bars=-1,
            )

    def test_h1_from_m15_uses_only_completed_m15_bars(self) -> None:
        m15_index = pd.date_range("2024-01-01 09:00", periods=8, freq="15min")
        m15 = _ohlcv_frame(m15_index, [1, 2, 3, 4, 5, 6, 7, 8])
        h1 = resample_completed_bars(m15, source_timeframe="M15", target_timeframe="H1")
        assert len(h1) == 2
        assert h1.iloc[0]["close"] == 4.0
        assert h1.iloc[1]["close"] == 8.0


class TestExogenousPreflight:
    def setup_method(self) -> None:
        clear_evaluation_frame_cache()

    def test_no_overlap_fails(self) -> None:
        primary_index = pd.date_range("2024-06-01", periods=5, freq="1h")
        primary = _ohlcv_frame(primary_index, [100.0] * 5)
        exog_index = pd.date_range("2024-01-01", periods=5, freq="1h")
        exog = _ohlcv_frame(exog_index, [1.0] * 5)
        loader = _loader_from_frames(
            {
                ("WIN$", "H1"): primary,
                ("WDO$", "H1"): exog,
            }
        )
        spec = ExogenousSeriesConfig(
            symbol="WDO$",
            source_timeframe="H1",
            recipes=["close"],
        )
        with pytest.raises(ValueError, match="overlap"):
            attach_exogenous_context(
                primary,
                [spec],
                primary_symbol="WIN$",
                primary_timeframe="H1",
                loader=loader,
                start=primary_index[0].to_pydatetime(),
                end=primary_index[-1].to_pydatetime(),
            )

    def test_large_gap_fails(self) -> None:
        idx = pd.to_datetime(["2024-01-01", "2024-03-15"], format="%Y-%m-%d")
        exog = _ohlcv_frame(idx, [1.0, 2.0])
        loader = _loader_from_frames({("WDO$", "H1"): exog})
        primary = _ohlcv_frame(pd.date_range("2024-03-14", periods=3, freq="1h"), [10.0] * 3)
        spec = ExogenousSeriesConfig(
            symbol="WDO$",
            source_timeframe="H1",
            recipes=["close"],
        )
        with pytest.raises(ValueError, match="gap"):
            attach_exogenous_context(
                primary,
                [spec],
                primary_symbol="WIN$",
                primary_timeframe="H1",
                loader=loader,
                start=primary.index[0].to_pydatetime(),
                end=primary.index[-1].to_pydatetime(),
            )


class TestExogenousCaching:
    def setup_method(self) -> None:
        clear_evaluation_frame_cache()

    def test_one_load_per_symbol_across_prepare_calls(self) -> None:
        primary_index = pd.date_range("2024-01-01", periods=24, freq="1h")
        primary = _ohlcv_frame(primary_index, [100 + i for i in range(24)])
        exog = _ohlcv_frame(primary_index, [50 + i * 0.1 for i in range(24)])
        loader = _loader_from_frames(
            {
                ("WIN$", "H1"): primary,
                ("WDO$", "H1"): exog,
            }
        )
        spec = ExogenousSeriesConfig(
            symbol="WDO$",
            source_timeframe="H1",
            recipes=["close", "return"],
            lookback_bars=4,
        )
        kwargs = dict(
            primary_symbol="WIN$",
            primary_timeframe="H1",
            start=primary_index[0].to_pydatetime(),
            end=primary_index[-1].to_pydatetime(),
            exogenous_series=[spec],
            loader=loader,
        )
        prepare_evaluation_frame(**kwargs)
        prepare_evaluation_frame(**kwargs)

        assert loader.calls.count(("WIN$", "H1", kwargs["start"], kwargs["end"])) == 1
        assert loader.calls.count(("WDO$", "H1", kwargs["start"], kwargs["end"])) == 1


class TestExogenousGenomeAndBacktest:
    def setup_method(self) -> None:
        clear_evaluation_frame_cache()

    def test_composite_reads_attached_exogenous_column(self) -> None:
        primary_index = pd.date_range("2024-01-01", periods=40, freq="1h")
        primary = _ohlcv_frame(primary_index, [100 + i for i in range(40)])
        exog = _ohlcv_frame(primary_index, [10 + i * 0.2 for i in range(40)])
        loader = _loader_from_frames({("WIN$", "H1"): primary, ("WDO$", "H1"): exog})
        frame, provenance = attach_exogenous_context(
            primary,
            [
                ExogenousSeriesConfig(
                    symbol="WDO$",
                    source_timeframe="H1",
                    recipes=["close"],
                )
            ],
            primary_symbol="WIN$",
            primary_timeframe="H1",
            loader=loader,
            start=primary_index[0].to_pydatetime(),
            end=primary_index[-1].to_pydatetime(),
        )
        assert provenance[0]["content_fingerprint"]
        col = exog_column_name("WDO$", "close")
        assert col in frame.columns
        assert frame[col].notna().any()

    def test_exogenous_context_does_not_change_traded_symbol(self) -> None:
        primary_index = pd.date_range("2024-01-01", periods=80, freq="1h")
        primary = _ohlcv_frame(
            primary_index,
            [100 + 5 * (i % 2) for i in range(80)],
        )
        exog = _ohlcv_frame(primary_index, [50 + i * 0.05 for i in range(80)])
        loader = _loader_from_frames({("WIN$", "H1"): primary, ("WDO$", "H1"): exog})
        frame, _ = attach_exogenous_context(
            primary,
            [
                ExogenousSeriesConfig(
                    symbol="WDO$",
                    source_timeframe="H1",
                    recipes=["close", "return"],
                    lookback_bars=4,
                )
            ],
            primary_symbol="WIN$",
            primary_timeframe="H1",
            loader=loader,
            start=primary_index[0].to_pydatetime(),
            end=primary_index[-1].to_pydatetime(),
        )
        genome = Genome.model_validate(
            {
                "version": 1,
                "genome_id": "primary-only-trades",
                "nodes": [
                    {"id": "s", "kind": "source.close", "params": {}, "inputs": []},
                    {
                        "id": "m",
                        "kind": "ind.ma",
                        "params": {"period": 5, "ma_type": "sma"},
                        "inputs": ["s"],
                    },
                    {"id": "x", "kind": "cmp.gt", "params": {}, "inputs": ["s", "m"]},
                    {"id": "never", "kind": "cmp.lt", "params": {}, "inputs": ["s", "s"]},
                ],
                "entry_long": {"ref": "x"},
                "entry_short": {"ref": "never"},
                "exit_long": {"ref": "never"},
                "exit_short": {"ref": "never"},
            }
        )
        strategy = build_strategy(
            "CompositeStrategy",
            {"genome": genome.model_dump(), "symbol": "WIN$"},
            "WIN$",
        )
        assert strategy.symbol == "WIN$"
        assert exog_column_name("WDO$", "close") in frame.columns
        assert exog_column_name("WDO$", "return_4") in frame.columns


class TestExogenousFeatureStore:
    def test_exog_close_matches_attached_column(self) -> None:
        primary_index = pd.date_range("2024-01-01", periods=40, freq="1h")
        primary = _ohlcv_frame(primary_index, [100 + i for i in range(40)])
        exog = _ohlcv_frame(primary_index, [10 + i * 0.2 for i in range(40)])
        loader = _loader_from_frames({("WIN$", "H1"): primary, ("WDO$", "H1"): exog})
        enriched, _ = attach_exogenous_context(
            primary,
            [
                ExogenousSeriesConfig(
                    symbol="WDO$",
                    source_timeframe="H1",
                    recipes=["close", "return"],
                    lookback_bars=4,
                )
            ],
            primary_symbol="WIN$",
            primary_timeframe="H1",
            loader=loader,
            start=primary_index[0].to_pydatetime(),
            end=primary_index[-1].to_pydatetime(),
        )
        bars = enriched.reset_index().rename(columns={"index": "time"})
        spec = get_feature_spec("exog_close")
        computed = compute_feature(bars, spec, {"symbol": "WDO$"})
        expected = enriched[exog_column_name("WDO$", "close")]
        pd.testing.assert_series_equal(
            computed.series.reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
        )


class TestExogenousConfigValidation:
    def test_ccm_has_no_required_exogenous(self) -> None:
        validate_exogenous_for_primary(
            primary_symbol="CCM$",
            primary_timeframe="H1",
            exogenous_series=[],
        )

    def test_win_wdo_mapping_allowed(self) -> None:
        validate_exogenous_for_primary(
            primary_symbol="WIN$",
            primary_timeframe="H1",
            exogenous_series=[
                ExogenousSeriesConfig(
                    symbol="WDO$",
                    source_timeframe="M15",
                    resampling_rule="last_completed",
                    target_timeframe="H1",
                    recipes=["close"],
                )
            ],
        )

    def test_disabled_exogenous_leaves_strategy_search_unchanged(self) -> None:
        config = StrategySearchConfig.model_validate(
            {
                "backtest": {
                    "symbol": "CCM$",
                    "timeframe": "H1",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-03-01T00:00:00",
                    "initial_capital": 10_000,
                    "point_value": 1.0,
                    "strategy": "CompositeStrategy",
                },
                "objective": {"mode": "maximize_net_profit"},
                "walkforward": {
                    "train_days": 30,
                    "test_days": 10,
                    "mode": "rolling",
                    "min_windows": 2,
                },
                "study": {
                    "name": "baseline",
                    "n_trials": 1,
                    "seed": 1,
                    "storage": {"type": "memory"},
                },
            }
        )
        assert config.exogenous_series == []
