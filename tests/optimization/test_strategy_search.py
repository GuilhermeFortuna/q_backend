from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.strategy_registry import list_registered_strategies
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.optimization.auto_search_space import derive_strategy_search_space
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
    DefaultBacktestRunner,
)
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    ObjectiveConfig,
    ObjectiveMode,
    SearchSpaceConfig,
    StudyConfig,
)
from q_backend.optimization.strategy_search import (
    CandidateResult,
    ExitPresetSearchConfig,
    GateConfig,
    METADATA_FIXED_PARAM_KEYS,
    RegistryCandidateProvider,
    SearchCandidate,
    StrategySearchConfig,
    StrategySearchRunner,
    _rank_results,
    _runner_for_candidate,
    evaluate_candidate,
)
from q_backend.optimization.walkforward import WalkForwardConfig


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)


def _make_intraday_ohlcv(start: datetime, days: int) -> pd.DataFrame:
    rows = []
    price = 100.0
    for day in range(days):
        for hour in (0, 6, 12, 18):
            timestamp = start + timedelta(days=day, hours=hour)
            drift = 0.1 if day % 10 < 5 else -0.05
            price = max(50.0, price + drift)
            rows.append(
                {
                    "time": timestamp,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 1000,
                }
            )
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def _bars_from_df(full_df: pd.DataFrame) -> list:
    class Bar:
        def __init__(self, row, timestamp):
            self._row = row
            self._timestamp = timestamp

        def model_dump(self):
            return {
                "time": self._timestamp,
                "open": self._row.open,
                "high": self._row.high,
                "low": self._row.low,
                "close": self._row.close,
                "volume": self._row.volume,
            }

    return [Bar(row, idx.to_pydatetime()) for idx, row in full_df.iterrows()]


def _narrow_risk_space() -> dict:
    return {
        "type": CategoricalParam(type="categorical", choices=["fixed_quantity"]),
        "quantity": FloatParam(type="float", low=1.0, high=1.0),
    }


def _narrow_search_space(strategy: str) -> SearchSpaceConfig:
    if strategy == "MACrossover":
        return SearchSpaceConfig(
            strategy_params={
                "short_period": IntParam(type="int", low=2, high=4),
                "long_period": IntParam(type="int", low=6, high=8),
            },
            risk_params=_narrow_risk_space(),
        )
    if strategy == "DonchianBreakout":
        return SearchSpaceConfig(
            strategy_params={
                "period": IntParam(type="int", low=2, high=8),
            },
            risk_params=_narrow_risk_space(),
        )
    if strategy == "VMA":
        return SearchSpaceConfig(
            strategy_params={
                "period": IntParam(type="int", low=2, high=8),
                "band_pct": FloatParam(type="float", low=0.0, high=0.5, step=0.1),
            },
            risk_params=_narrow_risk_space(),
        )
    raise ValueError(f"No narrow search space fixture for {strategy}")


class NarrowCandidateProvider:
    def __init__(self, strategies: list[str]) -> None:
        self._strategies = strategies

    def candidates(self):
        for name in self._strategies:
            _, fixed = derive_strategy_search_space(name)
            yield SearchCandidate(
                candidate_id=name,
                strategy=name,
                search_space=_narrow_search_space(name),
                fixed_params=fixed,
            )

    def report(self, results: list[CandidateResult]) -> None:
        return None


def _search_config(
    *,
    start: datetime,
    end: datetime,
    strategies: list[str] | None = None,
    n_trials: int = 2,
    gates: GateConfig | None = None,
) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": "TEST",
            "timeframe": "D1",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "initial_capital": 10_000.0,
            "point_value": 1.0,
            "strategy": "MACrossover",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(
            train_days=30,
            test_days=15,
            mode="rolling",
            min_windows=2,
            max_workers=1,
        ),
        study=StudyConfig(
            name="strategy_search_e2e",
            n_trials=n_trials,
            seed=42,
            storage={"type": "memory"},
        ),
        strategies=strategies,
        include_risk_search=False,
        gates=gates or GateConfig(min_completed_windows=1, min_oos_trades=1),
    )


CANDLE_STRATEGIES = [info.name for info in list_registered_strategies() if info.engine == "candle"]


def test_registry_provider_yields_candle_strategies_only():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
    )
    provider = RegistryCandidateProvider(config)
    names = [candidate.strategy for candidate in provider.candidates()]
    assert names == CANDLE_STRATEGIES
    assert "TickMaBreakout" in provider.unsupported_names()


def test_registry_provider_respects_explicit_subset():
    subset = ["MACrossover", "DonchianBreakout", "TickMaBreakout"]
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=subset,
    )
    provider = RegistryCandidateProvider(config)
    names = [candidate.strategy for candidate in provider.candidates()]
    assert names == ["MACrossover", "DonchianBreakout"]
    assert provider.unsupported_names() == ["TickMaBreakout"]


def test_registry_provider_default_config_yields_baseline_ids_only():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=["MACrossover", "VMA"],
    )
    provider = RegistryCandidateProvider(config)
    ids = [candidate.candidate_id for candidate in provider.candidates()]
    assert ids == ["MACrossover", "VMA"]
    assert provider.candidate_metadata() == {}


def test_registry_provider_exit_presets_expand_candidates():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=["MACrossover"],
    ).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=["fixed_pct_bracket", "atr_stop_chandelier"],
                include_baseline=True,
            )
        }
    )
    provider = RegistryCandidateProvider(config)
    ids = [candidate.candidate_id for candidate in provider.candidates()]
    assert ids == [
        "MACrossover",
        "MACrossover__exit_fixed_pct_bracket",
        "MACrossover__exit_atr_stop_chandelier",
    ]
    metadata = provider.candidate_metadata()
    assert metadata["MACrossover__exit_fixed_pct_bracket"]["exit_preset_id"] == "fixed_pct_bracket"
    assert metadata["MACrossover__exit_atr_stop_chandelier"]["exit_preset_label"] == ("ATR stop + Chandelier trail")


def test_registry_provider_exit_presets_without_baseline():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=["MACrossover"],
    ).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=["fixed_pct_bracket"],
                include_baseline=False,
            )
        }
    )
    provider = RegistryCandidateProvider(config)
    ids = [candidate.candidate_id for candidate in provider.candidates()]
    assert ids == ["MACrossover__exit_fixed_pct_bracket"]


def test_exit_presets_rejected_with_genetic_search():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 6, 1)
    with pytest.raises(ValueError, match="exit_presets is not supported with genetic"):
        StrategySearchConfig(
            backtest={
                "symbol": "TEST",
                "timeframe": "D1",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "initial_capital": 10_000.0,
                "point_value": 1.0,
                "strategy": "MACrossover",
            },
            objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
            walkforward=WalkForwardConfig(train_days=30, test_days=15, mode="rolling"),
            study=StudyConfig(name="genetic", n_trials=1),
            genetic={"population_size": 12, "generations": 3, "elite_count": 2},
            exit_presets=ExitPresetSearchConfig(enabled=True),
        )


def test_runner_strips_exit_preset_metadata_from_fixed_params():
    inner = MagicMock()
    inner.run.return_value = BacktestRunResult(metrics={"total_trades": 1, "net_profit": 1.0})
    runner = _runner_for_candidate(
        inner,
        {"short_period": 5, "_exit_preset_id": "fixed_pct_bracket"},
    )
    runner.run(
        BacktestRunConfig(
            symbol="TEST",
            timeframe="D1",
            start=_dt(2024, 1, 1),
            end=_dt(2024, 2, 1),
            initial_capital=10_000.0,
            point_value=1.0,
            strategy="MACrossover",
            strategy_params={"long_period": 20},
            position_sizing=None,
        )
    )
    call_config = inner.run.call_args[0][0]
    assert "_exit_preset_id" not in call_config.strategy_params
    assert call_config.strategy_params["short_period"] == 5
    assert call_config.strategy_params["long_period"] == 20
    assert METADATA_FIXED_PARAM_KEYS == frozenset({"_exit_preset_id"})


def test_evaluate_candidate_smoke_merges_exit_preset_params():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)

    captured_params: list[dict] = []

    class CaptureParamsRunner:
        def run(self, config: BacktestRunConfig) -> BacktestRunResult:
            captured_params.append(dict(config.strategy_params))
            return DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end]).run(config)

    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover"],
        n_trials=1,
    ).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=["fixed_pct_bracket"],
                include_baseline=False,
            )
        }
    )
    provider = RegistryCandidateProvider(config)
    candidate = next(provider.candidates())
    assert candidate.candidate_id == "MACrossover__exit_fixed_pct_bracket"
    assert candidate.fixed_params["_exit_preset_id"] == "fixed_pct_bracket"

    result = evaluate_candidate(
        candidate,
        config,
        CaptureParamsRunner(),
        ohlcv=full_df,
    )
    assert result.status in {"completed", "no_result", "error"}
    assert captured_params
    assert "_exit_preset_id" not in captured_params[0]


def test_strategy_search_result_includes_exit_preset_metadata():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover"],
        n_trials=1,
    ).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=["fixed_pct_bracket"],
                include_baseline=False,
            )
        }
    )
    provider = RegistryCandidateProvider(config)
    result = StrategySearchRunner(config, runner, provider=provider).run()
    assert result.candidate_metadata is not None
    assert "MACrossover__exit_fixed_pct_bracket" in result.candidate_metadata


def test_registry_provider_exit_presets_tick_strategy_unsupported():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=["TickMaBreakout"],
    ).model_copy(update={"exit_presets": ExitPresetSearchConfig(enabled=True)})
    provider = RegistryCandidateProvider(config)
    assert list(provider.candidates()) == []
    assert provider.unsupported_names() == ["TickMaBreakout"]


def test_registry_provider_all_presets_when_preset_ids_none():
    config = _search_config(
        start=_dt(2024, 1, 1),
        end=_dt(2024, 6, 1),
        strategies=["MACrossover"],
    ).model_copy(
        update={
            "exit_presets": ExitPresetSearchConfig(
                enabled=True,
                preset_ids=None,
                include_baseline=False,
            )
        }
    )
    provider = RegistryCandidateProvider(config)
    ids = [candidate.candidate_id for candidate in provider.candidates()]
    assert len(ids) == len(EXIT_PRESETS)
    assert all(candidate_id.startswith("MACrossover__exit_") for candidate_id in ids)


def test_strategy_search_end_to_end_ranks_by_oos_robustness():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])

    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover", "VMA"],
        n_trials=3,
    )
    provider = NarrowCandidateProvider(["MACrossover", "VMA"])

    result = StrategySearchRunner(config, runner, provider=provider).run()

    completed = [c for c in result.candidates if c.status == "completed"]
    assert len(completed) == 2
    ranked = [c for c in result.candidates if c.rank is not None]
    assert len(ranked) == 2
    assert ranked == sorted(
        completed,
        key=lambda candidate: candidate.robustness_score or float("-inf"),
        reverse=True,
    )
    assert result.best is ranked[0]
    assert ranked[0].robustness_score >= ranked[1].robustness_score


def test_single_get_ohlcv_across_multi_candidate_search():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    service = MagicMock()
    service.get_ohlcv.return_value = _bars_from_df(full_df)

    runner = DefaultBacktestRunner.from_market_data_sliced(
        service,
        symbol="TEST",
        timeframe="D1",
        start=start,
        end=end,
    )
    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover", "VMA"],
        n_trials=2,
    )
    provider = NarrowCandidateProvider(["MACrossover", "VMA"])

    StrategySearchRunner(config, runner, provider=provider).run()

    service.get_ohlcv.assert_called_once()


def test_failing_candidate_recorded_without_aborting_search():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    inner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])

    class FailOosRunner:
        def run(self, config: BacktestRunConfig) -> BacktestRunResult:
            window_days = (config.end - config.start).days
            if config.strategy == "VMA" and window_days <= 16:
                raise RuntimeError("forced candidate failure")
            return inner.run(config)

    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover", "VMA"],
        n_trials=3,
    )
    provider = NarrowCandidateProvider(["MACrossover", "VMA"])

    result = StrategySearchRunner(config, FailOosRunner(), provider=provider).run()

    statuses = {candidate.strategy: candidate.status for candidate in result.candidates}
    assert statuses["MACrossover"] == "completed"
    assert statuses["VMA"] == "error"
    assert result.best is not None
    assert result.best.strategy == "MACrossover"


def test_gated_candidate_sorted_after_passing():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover"],
        n_trials=3,
        gates=GateConfig(
            min_completed_windows=1,
            min_oos_trades=10_000,
            efficiency_low=0.3,
            efficiency_high=1.5,
        ),
    )
    provider = NarrowCandidateProvider(["MACrossover"])

    result = StrategySearchRunner(config, runner, provider=provider).run()
    candidate = result.candidates[0]

    assert candidate.status == "completed"
    assert candidate.passed_gates is False
    assert "few_oos_trades" in candidate.gate_flags
    assert candidate.rank is None
    assert result.best is None


def test_rank_results_orders_passing_before_gated_and_trailing():
    passing_high = CandidateResult(
        candidate_id="a",
        strategy="a",
        status="completed",
        passed_gates=True,
        objective_value=100.0,
        robustness_score=100.0,
    )
    passing_low = CandidateResult(
        candidate_id="b",
        strategy="b",
        status="completed",
        passed_gates=True,
        objective_value=50.0,
        robustness_score=50.0,
    )
    gated = CandidateResult(
        candidate_id="c",
        strategy="c",
        status="completed",
        passed_gates=False,
        objective_value=200.0,
        robustness_score=200.0,
        gate_flags=["few_oos_trades"],
    )
    no_result = CandidateResult(
        candidate_id="d",
        strategy="d",
        status="no_result",
    )
    error = CandidateResult(
        candidate_id="e",
        strategy="e",
        status="error",
        error="boom",
    )
    unsupported = CandidateResult(
        candidate_id="f",
        strategy="f",
        status="unsupported",
    )

    ranked = _rank_results([unsupported, error, no_result, gated, passing_low, passing_high])

    assert [candidate.candidate_id for candidate in ranked] == [
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
    ]
    assert ranked[0].rank == 1
    assert ranked[1].rank == 2
    assert all(candidate.rank is None for candidate in ranked[2:])


def test_rank_results_order_unchanged_with_exit_quality_diagnostics():
    passing_high = CandidateResult(
        candidate_id="a",
        strategy="a",
        status="completed",
        passed_gates=True,
        objective_value=100.0,
        robustness_score=100.0,
        diagnostics={
            "exit_quality": {
                "total_closed_trades": 10,
                "by_reason": {"signal": {"trades": 10, "total_pnl": 500.0, "win_rate": 0.6}},
            }
        },
    )
    passing_low = CandidateResult(
        candidate_id="b",
        strategy="b",
        status="completed",
        passed_gates=True,
        objective_value=50.0,
        robustness_score=50.0,
        diagnostics={
            "exit_quality": {
                "total_closed_trades": 5,
                "by_reason": {"fixed_sl": {"trades": 5, "total_pnl": -100.0, "win_rate": 0.0}},
            }
        },
    )

    ranked = _rank_results([passing_low, passing_high])

    assert [candidate.candidate_id for candidate in ranked] == ["a", "b"]


def test_evaluate_candidate_attaches_oos_exit_quality():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover"],
        n_trials=2,
    )
    candidate = next(NarrowCandidateProvider(["MACrossover"]).candidates())

    result = evaluate_candidate(
        candidate,
        config,
        runner,
        ohlcv=full_df,
    )

    assert result.status == "completed"
    assert result.diagnostics is not None
    exit_quality = result.diagnostics.get("exit_quality")
    assert exit_quality is not None
    assert exit_quality["total_closed_trades"] > 0
    assert isinstance(exit_quality.get("by_reason"), dict)
    assert len(result.oos_trades) == exit_quality["total_closed_trades"]


def test_should_stop_after_first_candidate():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _search_config(
        start=start,
        end=end,
        strategies=["MACrossover", "VMA"],
        n_trials=3,
    )
    provider = NarrowCandidateProvider(["MACrossover", "VMA"])

    stop_after_first = [False]

    def progress_callback(progress) -> None:
        if progress.phase == "done" and progress.current_candidate == 1:
            stop_after_first[0] = True

    def should_stop() -> bool:
        return stop_after_first[0]

    result = StrategySearchRunner(config, runner, provider=provider).run(
        progress_callback=progress_callback,
        should_stop=should_stop,
    )

    evaluated = [
        candidate for candidate in result.candidates if candidate.status in {"completed", "error", "no_result"}
    ]
    assert len(evaluated) == 1


def test_multi_objective_config_rejected():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 6, 1)
    with pytest.raises(ValueError, match="single-objective"):
        StrategySearchConfig(
            backtest={
                "symbol": "TEST",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            objective=ObjectiveConfig(mode=ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN),
            walkforward=WalkForwardConfig(train_days=10, test_days=5),
            study=StudyConfig(name="multi", n_trials=1),
        )


def test_tick_strategy_marked_unsupported_in_full_search():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 30)
    full_df = _make_intraday_ohlcv(start, 120)
    runner = DefaultBacktestRunner(data_provider=lambda cfg: full_df.loc[cfg.start : cfg.end])
    config = _search_config(start=start, end=end, strategies=["TickMaBreakout"], n_trials=1)

    result = StrategySearchRunner(config, runner).run()

    assert len(result.candidates) == 1
    assert result.candidates[0].status == "unsupported"
    assert result.best is None
