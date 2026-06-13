from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunResult,
    DefaultBacktestRunner,
)
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardProgress,
    WalkForwardRunner,
    split_windows,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)


def test_split_windows_rolling_geometry():
    cfg = WalkForwardConfig(train_days=10, test_days=5, mode="rolling", min_windows=1)
    start = _dt(2024, 1, 1)
    end = _dt(2024, 3, 1)
    windows = split_windows(start, end, cfg)

    assert windows[0].train_start == _dt(2024, 1, 1)
    assert windows[0].train_end == _dt(2024, 1, 11)
    assert windows[0].test_start == _dt(2024, 1, 11)
    assert windows[0].test_end == _dt(2024, 1, 16)

    assert windows[1].train_start == _dt(2024, 1, 6)
    assert windows[1].train_end == _dt(2024, 1, 16)
    assert windows[1].test_start == _dt(2024, 1, 16)
    assert windows[1].test_end == _dt(2024, 1, 21)


def test_split_windows_anchored_expands_train():
    cfg = WalkForwardConfig(train_days=10, test_days=5, mode="anchored", min_windows=1)
    start = _dt(2024, 1, 1)
    end = _dt(2024, 3, 1)
    windows = split_windows(start, end, cfg)

    assert windows[0].train_start == start
    assert windows[0].train_end == _dt(2024, 1, 11)

    assert windows[1].train_start == start
    assert windows[1].train_end == _dt(2024, 1, 16)


def test_split_windows_test_segments_tile_without_gaps():
    cfg = WalkForwardConfig(train_days=10, test_days=5, mode="rolling", min_windows=1)
    start = _dt(2024, 1, 1)
    end = _dt(2024, 3, 1)
    windows = split_windows(start, end, cfg)

    for left, right in zip(windows, windows[1:]):
        assert left.test_end == right.test_start

    assert windows[0].test_start == start + timedelta(days=10)
    for window in windows:
        assert window.test_start < window.test_end


def test_split_windows_keeps_partial_last_window():
    cfg = WalkForwardConfig(train_days=10, test_days=5, mode="rolling", min_windows=1)
    start = _dt(2024, 1, 1)
    end = _dt(2024, 1, 18)
    windows = split_windows(start, end, cfg)

    last = windows[-1]
    assert last.test_start == _dt(2024, 1, 16)
    assert last.test_end == end
    assert (last.test_end - last.test_start) >= timedelta(days=1)


def test_split_windows_raises_when_fewer_than_min_windows():
    cfg = WalkForwardConfig(train_days=30, test_days=30, mode="rolling", min_windows=2)
    start = _dt(2024, 1, 1)
    end = _dt(2024, 2, 15)

    with pytest.raises(ValueError, match="min_windows"):
        split_windows(start, end, cfg)


def _make_ohlcv_df(start: datetime, days: int) -> pd.DataFrame:
    rows = []
    price = 100.0
    for day in range(days):
        timestamp = start + timedelta(days=day)
        drift = 0.5 if day % 5 < 3 else -0.3
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


def _make_intraday_ohlcv_df(start: datetime, days: int) -> pd.DataFrame:
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


def _sliced_data_provider(full_df: pd.DataFrame):
    def data_provider(config: BacktestRunConfig) -> pd.DataFrame:
        return full_df.loc[config.start : config.end]

    return data_provider


def test_from_market_data_sliced_fetches_once_and_slices_per_window():
    start = _dt(2024, 1, 1)
    end = _dt(2024, 4, 1)
    full_df = _make_ohlcv_df(start, 90)

    class Bar:
        def __init__(self, row):
            self._row = row

        def model_dump(self):
            return self._row

    bars = [
        Bar(
            {
                "time": idx.to_pydatetime(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
        )
        for idx, row in full_df.iterrows()
    ]

    service = MagicMock()
    service.get_ohlcv.return_value = bars

    runner = DefaultBacktestRunner.from_market_data_sliced(
        service,
        symbol="TEST",
        timeframe="D1",
        start=start,
        end=end,
    )

    slice_a = runner._fetch_data(
        BacktestRunConfig(
            symbol="TEST",
            timeframe="D1",
            start=_dt(2024, 1, 1),
            end=_dt(2024, 1, 31),
            initial_capital=10_000.0,
            point_value=1.0,
            strategy="MACrossover",
            strategy_params={},
            position_sizing=None,
        )
    )
    slice_b = runner._fetch_data(
        BacktestRunConfig(
            symbol="TEST",
            timeframe="D1",
            start=_dt(2024, 2, 1),
            end=_dt(2024, 2, 29),
            initial_capital=10_000.0,
            point_value=1.0,
            strategy="MACrossover",
            strategy_params={},
            position_sizing=None,
        )
    )

    service.get_ohlcv.assert_called_once_with("TEST", "D1", start, end)
    assert len(slice_a) == 31
    assert len(slice_b) == 29
    assert slice_a.index.min() == pd.Timestamp(_dt(2024, 1, 1))
    assert slice_b.index.min() == pd.Timestamp(_dt(2024, 2, 1))


def _walkforward_optimization_config(
    *,
    start: datetime,
    end: datetime,
    n_trials: int = 3,
) -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {
                "name": "wf_e2e",
                "n_trials": n_trials,
                "seed": 42,
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "timeframe": "D1",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "initial_capital": 10_000.0,
                "point_value": 1.0,
                "strategy": "MACrossover",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 4},
                    "long_period": {"type": "int", "low": 6, "high": 8},
                },
                "risk_params": {
                    "type": {
                        "type": "categorical",
                        "choices": ["fixed_quantity"],
                    },
                    "quantity": {"type": "float", "low": 1.0, "high": 1.0},
                },
            },
        }
    )


def test_walkforward_runner_end_to_end():
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    full_df = _make_intraday_ohlcv_df(start, 120)
    config = _walkforward_optimization_config(start=start, end=end, n_trials=3)
    wf_config = WalkForwardConfig(
        train_days=30, test_days=15, mode="rolling", min_windows=2
    )

    runner = DefaultBacktestRunner(data_provider=_sliced_data_provider(full_df))
    progress: list[WalkForwardProgress] = []

    result = WalkForwardRunner(config, wf_config, runner).run(
        progress_callback=progress.append
    )

    completed = [window for window in result.windows if window.status == "completed"]
    assert len(completed) >= 2
    assert len(progress) == len(result.windows) * 2

    for window in completed:
        assert window.best_params
        assert window.is_metrics is not None
        assert window.oos_metrics is not None
        for trade in window.oos_trades:
            assert trade.exit_time is not None
            assert window.test_start <= trade.exit_time <= window.test_end

    assert result.oos_equity_curve.iloc[0] == pytest.approx(
        config.backtest.initial_capital
    )
    assert result.oos_metrics.get("total_trades", 0) > 0
    assert result.efficiency is not None


def test_walkforward_runner_skips_no_result_window_without_aborting():
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    full_df = _make_intraday_ohlcv_df(start, 120)
    config = _walkforward_optimization_config(start=start, end=end, n_trials=2)
    wf_config = WalkForwardConfig(
        train_days=30, test_days=15, mode="rolling", min_windows=2
    )
    windows = split_windows(start, end, wf_config)
    fail_train_start = windows[0].train_start
    fail_train_end = windows[0].train_end
    inner = DefaultBacktestRunner(data_provider=_sliced_data_provider(full_df))

    @dataclass
    class SelectiveRunner:
        inner: DefaultBacktestRunner
        calls: list[BacktestRunConfig] = field(default_factory=list)

        def run(self, config: BacktestRunConfig) -> BacktestRunResult:
            self.calls.append(config)
            if config.start == fail_train_start and config.end == fail_train_end:
                return BacktestRunResult(
                    metrics={"total_trades": 0, "total_pnl": 0.0},
                    trades=[],
                )
            return self.inner.run(config)

    runner = SelectiveRunner(inner=inner)

    result = WalkForwardRunner(config, wf_config, runner).run()

    assert result.windows[0].status == "no_result"
    assert any(window.status == "completed" for window in result.windows[1:])
    assert result.oos_equity_curve.iloc[0] == pytest.approx(
        config.backtest.initial_capital
    )


def test_from_market_data_sliced_single_fetch_across_walkforward_run():
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    full_df = _make_intraday_ohlcv_df(start, 120)

    class Bar:
        def __init__(self, row):
            self._row = row

        def model_dump(self):
            return self._row

    bars = [
        Bar(
            {
                "time": idx.to_pydatetime(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
        )
        for idx, row in full_df.iterrows()
    ]

    service = MagicMock()
    service.get_ohlcv.return_value = bars

    backtest_runner = DefaultBacktestRunner.from_market_data_sliced(
        service,
        symbol="TEST",
        timeframe="D1",
        start=start,
        end=end,
    )
    config = _walkforward_optimization_config(start=start, end=end, n_trials=2)
    wf_config = WalkForwardConfig(
        train_days=30, test_days=15, mode="rolling", min_windows=2
    )

    WalkForwardRunner(config, wf_config, backtest_runner).run()

    service.get_ohlcv.assert_called_once()


def test_walkforward_parallel_matches_sequential():
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 30)
    full_df = _make_intraday_ohlcv_df(start, 120)
    config = _walkforward_optimization_config(start=start, end=end, n_trials=3)
    wf_seq = WalkForwardConfig(
        train_days=30, test_days=15, mode="rolling", min_windows=2, max_workers=1
    )
    wf_par = WalkForwardConfig(
        train_days=30, test_days=15, mode="rolling", min_windows=2, max_workers=2
    )

    runner = DefaultBacktestRunner(data_provider=_sliced_data_provider(full_df))
    seq = WalkForwardRunner(config, wf_seq, runner).run()

    progress: list[WalkForwardProgress] = []
    par = WalkForwardRunner(config, wf_par, runner, ohlcv=full_df).run(
        progress_callback=progress.append
    )

    assert [w.index for w in par.windows] == [w.index for w in seq.windows]
    assert [w.status for w in par.windows] == [w.status for w in seq.windows]
    par_completed = [w for w in par.windows if w.status == "completed"]
    for par_w, seq_w in zip(
        par_completed, [w for w in seq.windows if w.status == "completed"]
    ):
        assert par_w.best_params == seq_w.best_params
        assert par_w.oos_metrics == seq_w.oos_metrics
    assert par.efficiency == pytest.approx(seq.efficiency)

    # Parallel path reports a monotonic completion count, one tick per window.
    assert [p.windows_completed for p in progress] == list(
        range(1, len(par.windows) + 1)
    )


def test_walkforward_rejects_tick_engine():
    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "tick_wf", "n_trials": 1, "storage": {"type": "memory"}},
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "TEST",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
                "engine": "tick",
            },
            "search_space": {},
        }
    )
    wf_config = WalkForwardConfig(train_days=10, test_days=5)

    with pytest.raises(ValueError, match="candle engine only"):
        WalkForwardRunner(
            config,
            wf_config,
            DefaultBacktestRunner(data_provider=lambda _cfg: pd.DataFrame()),
        )
