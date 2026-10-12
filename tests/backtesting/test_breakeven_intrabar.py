"""Breakeven protects unarmed trades and fills against ordered trade prices."""

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.candle_kernel import _exit_params
from q_backend.backtesting.exit_rules.registry import all_param_specs
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.signal_columns import write_signal_columns


class EntryStrategy(TradingStrategy):
    def __init__(self, side=1, **params):
        super().__init__(**params)
        self.side = side
        self.symbol = "TEST"

    def compute_indicators(self, data):
        signal = pd.Series(False, index=data.index)
        signal.iloc[0] = True
        absent = pd.Series(False, index=data.index)
        return write_signal_columns(
            data.copy(),
            entry_long=signal if self.side == 1 else absent,
            entry_short=signal if self.side == -1 else absent,
            exit_long=False,
            exit_short=False,
        )

    def get_chart_indicators(self):
        return []


def test_enabled_breakeven_normalizes_missing_emergency_distance_to_100_ticks():
    assert _exit_params({"breakeven_trigger_pct": 0.02}) == {
        "breakeven_trigger_pct": 0.02,
        "breakeven_emergency_stop_ticks": 100,
    }


def test_catalog_defaults_protect_an_enabled_breakeven_rule():
    defaults = {spec.name: spec.default for spec in all_param_specs()}
    defaults["breakeven_trigger_pct"] = 0.02
    assert _exit_params(defaults)["breakeven_emergency_stop_ticks"] == 100


def test_explicit_zero_disables_only_the_emergency_distance():
    assert _exit_params({"breakeven_trigger_pct": 0.02, "breakeven_emergency_stop_ticks": 0}) == {
        "breakeven_trigger_pct": 0.02,
        "breakeven_emergency_stop_ticks": 0,
    }


class TickProvider:
    def __init__(self, prices, times=None):
        self.prices = prices
        start = pd.Timestamp("2024-01-02 10:00").value // 1_000_000
        self.times = times if times is not None else start + np.arange(len(prices)) * 1000

    def get_ticks_columnar(self, symbol, start, end, **kwargs):
        return {"last": np.asarray(self.prices, dtype=float), "time_msc": np.asarray(self.times, dtype=np.int64)}


def replay_frame():
    return pd.DataFrame(
        {"open": [100.0], "high": [103.0], "low": [74.0], "close": [102.0]},
        index=pd.DatetimeIndex(["2024-01-02 10:00"]),
    )


def test_market_replay_sorts_prices_stably_and_returns_wall_clock_tick_times():
    from q_backend.backtesting.intrabar import MarketTickReplay

    start = pd.Timestamp("2024-01-02 10:00").value // 1_000_000
    replay = MarketTickReplay(
        TickProvider([100, 103, 74, 102], [start, start + 2000, start + 1000, start + 3000]),
        "TEST",
        "H1",
        replay_frame(),
    )
    times, prices = replay(0)
    assert prices.tolist() == [100, 74, 103, 102]
    assert times.tolist() == [start * 1000, (start + 1000) * 1000, (start + 2000) * 1000, (start + 3000) * 1000]


@pytest.mark.parametrize("prices", [[], [100, 103, 102]])
def test_market_replay_rejects_missing_or_mismatched_tick_history(prices, monkeypatch):
    from q_backend.backtesting import intrabar
    from q_backend.backtesting.intrabar import MarketTickReplay

    monkeypatch.setattr(intrabar.time, "sleep", lambda _: None)
    replay = MarketTickReplay(TickProvider(prices), "TEST", "H1", replay_frame())
    with pytest.raises(ValueError, match="ticks"):
        replay(0)


def test_market_replay_rereads_a_candle_the_provider_first_returned_empty(monkeypatch):
    from q_backend.backtesting import intrabar
    from q_backend.backtesting.intrabar import MarketTickReplay

    monkeypatch.setattr(intrabar.time, "sleep", lambda _: None)
    provider = TickProvider([100, 74, 103, 102])
    served = provider.get_ticks_columnar
    reads = []

    def cold_then_warm(symbol, start, end, **kwargs):
        reads.append(1)
        if len(reads) == 1:
            return {"last": np.array([]), "time_msc": np.array([], dtype=np.int64)}
        return served(symbol, start, end, **kwargs)

    provider.get_ticks_columnar = cold_then_warm
    _, prices = MarketTickReplay(provider, "TEST", "H1", replay_frame())(0)

    assert prices.tolist() == [100, 74, 103, 102]
    assert len(reads) == 2


def test_market_replay_excludes_the_next_bar_boundary():
    from q_backend.backtesting.intrabar import MarketTickReplay

    start = pd.Timestamp("2024-01-02 10:00").value // 1_000_000
    replay = MarketTickReplay(
        TickProvider([100, 74, 103, 102, 1], [start, start + 1000, start + 2000, start + 3000, start + 3600000]),
        "TEST",
        "H1",
        replay_frame(),
    )
    _, prices = replay(0)
    assert prices.tolist() == [100, 74, 103, 102]


@pytest.mark.parametrize(
    "side,prices,reason,fill",
    [
        (1, [100, 99, 75], "breakeven_emergency", 75),
        (-1, [100, 101, 125], "breakeven_emergency", 125),
        (1, [100, 99, 103, 100], "breakeven", 100),
        (-1, [100, 101, 97, 100], "breakeven", 100),
    ],
)
def test_engine_default_emergency_and_breakeven_fill_at_the_crossing_tick(side, prices, reason, fill):
    frame = pd.DataFrame(
        {
            "open": [100, 100],
            "high": [100, max(prices)],
            "low": [100, min(prices)],
            "close": [100, prices[-1]],
        },
        index=pd.date_range("2024-01-02 09:00", periods=2, freq="h"),
    )
    from q_backend.backtesting.intrabar import MarketTickReplay

    engine = BacktestEngine(
        EntryStrategy(side, breakeven_trigger_pct=0.02),
        FixedQuantitySizer(1),
        intrabar_factory=lambda chunk: MarketTickReplay(TickProvider(prices), "TEST", "H1", chunk),
        tick_size=0.25,
    )
    trades = engine.run(frame).get_closed_trades()
    assert len(trades) == 1
    assert trades[0].exit_reason == reason
    assert trades[0].exit_price == fill
    assert trades[0].exit_time == frame.index[1]
    assert trades[0].exit_tick_time == pd.Timestamp("2024-01-02 10:00", tz="America/Sao_Paulo") + pd.Timedelta(
        seconds=len(prices) - 1
    )


def test_bar_only_exit_adapter_refuses_intrabar_breakeven():
    from q_backend.backtesting.exit_strategy import ExitStrategy
    from q_backend.backtesting.models import Trade, SignalAction

    stop = ExitStrategy(breakeven_trigger_pct=0.02)
    trade = Trade(
        id="t",
        order_id="o",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1,
        entry_price=100,
        entry_time=pd.Timestamp("2024-01-02"),
    )
    with pytest.raises(ValueError, match="intrabar"):
        stop.check_exits([trade], pd.Series({"close": 100, "high": 103, "low": 99}))


def test_stack_job_uses_instrument_tick_size_and_returns_intrabar_exit(monkeypatch):
    from q_backend.api import backtest_jobs
    from q_backend.backtesting.costs import TransactionCostConfig
    from q_backend.market_data.models import OHLCV

    class Service(TickProvider):
        def get_symbol_info(self, symbol):
            return {"trade_tick_size": 0.25}

        def get_ohlcv(self, symbol, timeframe, start, end):
            return [
                OHLCV(time=pd.Timestamp("2024-01-02 09:00"), open=100, high=100, low=100, close=100, tick_volume=1),
                OHLCV(time=pd.Timestamp("2024-01-02 10:00"), open=100, high=100, low=74, close=74, tick_volume=3),
            ]

    # Control the entry decision; retain the actual worker, sizing, replay and Rust fill path.
    monkeypatch.setattr(backtest_jobs, "build_composite_entry", lambda *args: EntryStrategy(breakeven_trigger_pct=0.02))
    request = backtest_jobs.BacktestJobRequest(
        symbol="TEST",
        timeframe="H1",
        entries=[{"strategy": "MACrossover", "params": {}}],
        point_value=12.5,
        exit_params={"breakeven_trigger_pct": 0.02},
        costs=TransactionCostConfig(cost_per_contract=2.5),
    )
    payload, trades, _, _ = backtest_jobs._execute_candle(
        request, pd.Timestamp("2024-01-02 09:00"), pd.Timestamp("2024-01-02 11:00"), Service([100, 99, 74])
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == "breakeven_emergency"
    assert trades[0].exit_price == 74
    assert trades[0].pnl == -330
    assert payload["metrics"]["total_trades"] == 1
    assert backtest_jobs._request_config(request)["exit_params"]["breakeven_emergency_stop_ticks"] == 100


def test_an_older_engine_is_rejected_instead_of_ignoring_the_emergency_stop(monkeypatch):
    from q_backend.backtesting.candle_kernel import engine as kernel

    monkeypatch.setattr(kernel, "BREAKEVEN_INTRABAR", False)
    backtest = BacktestEngine(EntryStrategy(breakeven_trigger_pct=0.02), FixedQuantitySizer(1))
    with pytest.raises(ImportError, match="BREAKEVEN_INTRABAR"):
        backtest.run(replay_frame())


@pytest.mark.parametrize("injected", [True, False])
def test_sliced_optimization_runner_uses_the_same_intrabar_stop(monkeypatch, injected):
    from q_backend.optimization import backtest_runner

    class Service(TickProvider):
        def get_symbol_info(self, symbol):
            return {"trade_tick_size": 0.25}

    frame = pd.DataFrame(
        {"open": [100, 100], "high": [100, 100], "low": [100, 74], "close": [100, 74]},
        index=pd.date_range("2024-01-02 09:00", periods=2, freq="h"),
    )
    monkeypatch.setattr(backtest_runner, "build_strategy", lambda *args: EntryStrategy(breakeven_trigger_pct=0.02))
    service = Service([100, 99, 74])
    monkeypatch.setattr("q_backend.market_data.service.MarketDataService", lambda: service)
    runner = backtest_runner.DefaultBacktestRunner.from_frame_sliced(
        frame, market_data_service=service if injected else None
    )
    config = backtest_runner.BacktestRunConfig(
        symbol="TEST",
        timeframe="H1",
        start=frame.index[0],
        end=frame.index[-1],
        initial_capital=10000,
        point_value=1,
        strategy="TEST",
        strategy_params={},
        position_sizing=None,
    )
    result = runner.run(config)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "breakeven_emergency"
    assert result.trades[0].exit_price == 74
