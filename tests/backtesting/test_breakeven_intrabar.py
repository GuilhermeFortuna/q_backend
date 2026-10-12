"""Breakeven protects unarmed trades and fills against ordered trade prices."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.candle_kernel import _exit_params
from q_backend.backtesting.intrabar import MarketTickReplay
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


_TRADE_FLAGS = 8 | 16 | 32 | 64


def minute_bar(minute, open_, high, low, close, volume):
    from q_backend.market_data.models import OHLCV

    return OHLCV(
        time=pd.Timestamp(minute),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=1,
        real_volume=volume,
    )


class TickProvider:
    """Serves deals and, unless given, M1 bars that the deals themselves reproduce."""

    def __init__(self, prices, times=None, volumes=None, flags=None, bars=None):
        self.prices = prices
        start = pd.Timestamp("2024-01-02 10:00").value // 1_000_000
        self.times = times if times is not None else start + np.arange(len(prices)) * 1000
        self.volumes = volumes if volumes is not None else [1] * len(prices)
        self.flags = flags
        self.bars = bars

    def get_ticks_columnar(self, symbol, start, end, **kwargs):
        arrays = {
            "last": np.asarray(self.prices, dtype=float),
            "time_msc": np.asarray(self.times, dtype=np.int64),
            "volume": np.asarray(self.volumes, dtype=float),
        }
        if self.flags is not None:
            arrays["flags"] = np.asarray(self.flags, dtype=np.int64)
        return arrays

    def get_ohlcv(self, symbol, timeframe, start, end):
        if self.bars is not None:
            return [bar for bar in self.bars if start <= bar.time < end]
        times = np.asarray(self.times, dtype=np.int64)
        prices = np.asarray(self.prices, dtype=float)
        volumes = np.asarray(self.volumes, dtype=float)
        keep = np.ones(times.size, dtype=bool) if self.flags is None else (np.asarray(self.flags) & _TRADE_FLAGS) != 0
        order = np.argsort(times[keep], kind="stable")
        times, prices, volumes = times[keep][order], prices[keep][order], volumes[keep][order]
        bars = []
        for minute in np.unique(times // 60_000 * 60_000):
            chosen = (times // 60_000 * 60_000) == minute
            px = prices[chosen]
            bar_time = pd.Timestamp(int(minute), unit="ms")
            bars.append(minute_bar(bar_time, px[0], px.max(), px.min(), px[-1], int(volumes[chosen].sum())))
        return [bar for bar in bars if start <= bar.time < end]


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
            return {"last": np.array([]), "time_msc": np.array([], dtype=np.int64), "volume": np.array([])}
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
    # The M1 bar has no real volume, so its prices (which aggregate to the candle) stand in for the minute.
    assert trades[0].exit_resolution == "minute"
    assert [note["code"] for note in payload["notes"]] == ["intrabar_incomplete_ticks"]
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


def test_market_replay_ignores_quote_updates_that_repeat_the_previous_last():
    from q_backend.backtesting.intrabar import MarketTickReplay

    # The first quote update carries the previous candle's close (99); only deals count.
    provider = TickProvider([99, 100, 74, 103, 103, 102], flags=[2, 8, 8, 4, 24, 8])
    _, prices = MarketTickReplay(provider, "TEST", "H1", replay_frame())(0)

    assert prices.tolist() == [100, 74, 103, 102]


def test_market_replay_snaps_rounded_terminal_prices_onto_the_tick_grid():
    from q_backend.backtesting.intrabar import MarketTickReplay

    frame = replay_frame()
    frame.loc[frame.index[0], ["open", "high", "low", "close"]] = [100.25, 103.75, 74.0, 102.5]
    bars = [minute_bar("2024-01-02 10:00", 100.25, 103.75, 74.0, 102.5, 4)]
    provider = TickProvider([100.3, 74.0, 103.8, 102.5], bars=bars)

    _, prices = MarketTickReplay(provider, "TEST", "H1", frame, tick_size=0.25)(0)

    assert prices.tolist() == [100.25, 74.0, 103.75, 102.5]


START = pd.Timestamp("2024-01-02 10:00")


def ms(offset_seconds):
    return (START + pd.Timedelta(seconds=offset_seconds)).value // 1_000_000


def three_minute_frame():
    return pd.DataFrame(
        {"open": [100.0], "high": [110.0], "low": [74.0], "close": [106.0]},
        index=pd.DatetimeIndex([START]),
    )


def test_complete_deals_yield_exactly_those_deals_without_substitution():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # Minute 10:00: 100, 110, 100 (vol 3, close 100). Minute 10:01: 106, 104 (vol 3, close 104).
    deal_times = [ms(0), ms(20), ms(40), ms(60), ms(80)]
    prices = [100, 110, 100, 106, 104]
    bars = [
        minute_bar("2024-01-02 10:00", 100, 110, 100, 100, 3),
        minute_bar("2024-01-02 10:01", 106, 106, 104, 104, 2),
    ]
    frame = pd.DataFrame({"open": [100.0], "high": [110.0], "low": [100.0], "close": [104.0]}, index=[START])
    report = ReplayReport()
    provider = TickProvider(prices, times=deal_times, volumes=[1, 1, 1, 1, 1], bars=bars)

    times, out = MarketTickReplay(provider, "TEST", "H1", frame, report=report)(0)

    assert out.tolist() == prices
    assert times.tolist() == [t * 1000 for t in deal_times]
    assert report.substituted == set()
    assert report.resolutions == {}


def test_a_deal_just_after_a_minute_boundary_belongs_to_the_previous_minute_by_volume():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # 10:01:00.300 trades at 105 and closes minute 10:00 (vol 3, last 105); the 10:01 bar follows.
    deal_times = [ms(0), ms(30), ms(60.3), ms(70), ms(80)]
    prices = [100, 110, 105, 106, 104]
    volumes = [1, 1, 1, 2, 1]
    bars = [
        minute_bar("2024-01-02 10:00", 100, 110, 100, 105, 3),
        minute_bar("2024-01-02 10:01", 106, 106, 104, 104, 3),
    ]
    report = ReplayReport()
    provider = TickProvider(prices, times=deal_times, volumes=volumes, bars=bars)
    frame = pd.DataFrame({"open": [100.0], "high": [110.0], "low": [100.0], "close": [104.0]}, index=[START])

    _, out = MarketTickReplay(provider, "TEST", "H1", frame, report=report)(0)

    assert out.tolist() == prices
    assert report.resolutions == {}


def test_a_closing_auction_deal_minutes_after_the_last_bar_is_assigned_to_it():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # The last bar of the day is 10:00; an auction deal at 10:09 at its close belongs to it.
    deal_times = [ms(0), ms(30), ms(9 * 60)]
    prices = [100, 110, 105]
    bars = [minute_bar("2024-01-02 10:00", 100, 110, 100, 105, 3)]
    frame = pd.DataFrame({"open": [100.0], "high": [110.0], "low": [100.0], "close": [105.0]}, index=[START])
    report = ReplayReport()

    _, out = MarketTickReplay(TickProvider(prices, times=deal_times, bars=bars), "TEST", "H1", frame, report=report)(0)

    assert out.tolist() == prices
    assert report.substituted == set()


def test_one_missing_minute_is_substituted_in_order_and_the_others_keep_their_deals():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # Minute 2 (10:01) has no deals; its bar is 100 open, 90 low, 112 high, 104 close.
    deal_times = [ms(0), ms(30), ms(120), ms(150)]
    prices = [100, 108, 106, 105]
    bars = [
        minute_bar("2024-01-02 10:00", 100, 108, 100, 108, 2),
        minute_bar("2024-01-02 10:01", 100, 112, 90, 104, 0),
        minute_bar("2024-01-02 10:02", 106, 106, 105, 105, 2),
    ]
    frame = pd.DataFrame({"open": [100.0], "high": [112.0], "low": [90.0], "close": [105.0]}, index=[START])
    report = ReplayReport()

    times, out = MarketTickReplay(
        TickProvider(prices, times=deal_times, bars=bars), "TEST", "H1", frame, report=report
    )(0)

    # Close 104 >= open 100: open, low, high, close, stamped at 0/15/30/45 s into the minute.
    assert out.tolist() == [100, 108, 100, 90, 112, 104, 106, 105]
    assert times.tolist()[2:6] == [ms(60) * 1000, ms(75) * 1000, ms(90) * 1000, ms(105) * 1000]
    assert report.substituted == {START}
    assert sorted(report.resolutions.values()) == ["minute"] * 4


def test_a_down_minute_substitutes_open_high_low_close():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    bars = [minute_bar("2024-01-02 10:00", 100, 112, 90, 95, 0)]
    frame = pd.DataFrame({"open": [100.0], "high": [112.0], "low": [90.0], "close": [95.0]}, index=[START])
    report = ReplayReport()

    _, out = MarketTickReplay(TickProvider([], bars=bars), "TEST", "H1", frame, report=report)(0)

    assert out.tolist() == [100, 112, 90, 95]


def test_with_no_deals_every_minute_is_substituted_and_the_run_completes(monkeypatch):
    from q_backend.backtesting import intrabar
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    monkeypatch.setattr(intrabar.time, "sleep", lambda _: None)
    bars = [
        minute_bar("2024-01-02 10:00", 100, 108, 100, 108, 0),
        minute_bar("2024-01-02 10:01", 108, 110, 104, 104, 0),
    ]
    frame = pd.DataFrame({"open": [100.0], "high": [110.0], "low": [100.0], "close": [104.0]}, index=[START])
    report = ReplayReport()

    times, out = MarketTickReplay(TickProvider([], times=[], bars=bars), "TEST", "H1", frame, report=report)(0)

    assert out.size == 8
    assert len(set(times.tolist())) == 8
    assert out[0] == 100 and out[-1] == 104
    assert len(report.resolutions) == 8
    assert set(report.resolutions.values()) == {"minute"}


def test_without_usable_m1_bars_the_candle_prices_are_used_and_marked_candle():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    report = ReplayReport()
    provider = TickProvider([100, 74, 103, 102], bars=[])
    times, out = MarketTickReplay(provider, "TEST", "H1", replay_frame(), report=report)(0)

    # Close 102 >= open 100: open, low, high, close, spread over the hour.
    assert out.tolist() == [100, 74, 103, 102]
    assert times.tolist() == [(ms(0) + q) * 1000 for q in (0, 900_000, 1_800_000, 2_700_000)]
    assert set(report.resolutions.values()) == {"candle"}


def test_m1_bars_that_do_not_aggregate_to_the_candle_fall_back_to_the_candle():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    bars = [minute_bar("2024-01-02 10:00", 100, 103, 80, 102, 3)]
    report = ReplayReport()
    provider = TickProvider([100, 74, 103, 102], bars=bars)

    _, out = MarketTickReplay(provider, "TEST", "H1", replay_frame(), report=report)(0)

    assert out.tolist() == [100, 74, 103, 102]
    assert set(report.resolutions.values()) == {"candle"}


def test_a_provider_connection_error_still_fails_the_run():
    from q_backend.backtesting.intrabar import MarketTickReplay

    class Down(TickProvider):
        def get_ticks_columnar(self, symbol, start, end, **kwargs):
            raise ConnectionError("gateway unreachable")

    bars = [minute_bar("2024-01-02 10:00", 100, 103, 74, 102, 4)]
    with pytest.raises(ConnectionError):
        MarketTickReplay(Down([100], bars=bars), "TEST", "H1", replay_frame())(0)


def test_exit_resolution_and_note_follow_the_tick_that_decided_each_exit():
    from q_backend.backtesting.intrabar import (
        ReplayReport,
        classify_exit_resolutions,
        intrabar_notes,
    )
    from q_backend.backtesting.models import SignalAction, Trade

    report = ReplayReport()
    report.replayed.update({START, START + pd.Timedelta(hours=1)})
    report.substituted.add(START)
    substituted_time = pd.Timestamp("2024-01-02 10:00:15")
    report.resolutions[int(substituted_time.value // 1000)] = "minute"

    def trade(exit_tick_time, pnl):
        return Trade(
            id=str(pnl),
            order_id="o",
            symbol="TEST",
            action=SignalAction.BUY,
            quantity=1,
            entry_time=START,
            entry_price=100,
            exit_time=START,
            exit_price=100,
            pnl=pnl,
            exit_tick_time=exit_tick_time,
        )

    trades = [
        trade(None, 5.0),
        trade(substituted_time.tz_localize("America/Sao_Paulo"), -320.0),
        trade(pd.Timestamp("2024-01-02 10:00:20").tz_localize("America/Sao_Paulo"), 10.0),
    ]
    classify_exit_resolutions(trades, report)

    assert [t.exit_resolution for t in trades] == [None, "minute", "tick"]
    [note] = intrabar_notes(trades, report)
    assert note["code"] == "intrabar_incomplete_ticks"
    assert note["details"]["minute_exits"] == 1
    assert note["details"]["tick_exits"] == 1
    assert note["details"]["bar_exits"] == 1
    assert note["details"]["candles_replayed"] == 2
    assert note["details"]["candles_substituted"] == 1
    assert note["details"]["session_dates"] == ["2024-01-02"]
    assert note["details"]["substituted_net_pnl"] == -320.0
    assert "R$ -320.00" in note["message"]
    assert note["message"] == (
        "3 trades: 1 exited on real ticks, 1 used normal candle-based exits. "
        "Incomplete or inconsistent tick history required substituted exits: "
        "1 at one-minute resolution (net R$ -320.00 on the substituted exits)."
    )


def test_no_note_when_nothing_was_substituted():
    from q_backend.backtesting.intrabar import ReplayReport, intrabar_notes

    assert intrabar_notes([], ReplayReport()) == []


def adjusted_minutes():
    """Two minutes of unadjusted deals: 10:00 trades 100, 110, 100 and 10:01 trades 106, 104."""
    return [ms(0), ms(20), ms(40), ms(60), ms(80)], [100.0, 110.0, 100.0, 106.0, 104.0]


def test_deals_of_a_series_adjusted_by_difference_are_shifted_onto_its_bars():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    deal_times, prices = adjusted_minutes()
    bars = [
        minute_bar("2024-01-02 10:00", 744.25, 754.25, 744.25, 744.25, 3),
        minute_bar("2024-01-02 10:01", 750.25, 750.25, 748.25, 748.25, 2),
    ]
    frame = pd.DataFrame({"open": [744.25], "high": [754.25], "low": [744.25], "close": [748.25]}, index=[START])
    report = ReplayReport()

    times, out = MarketTickReplay(
        TickProvider(prices, times=deal_times, bars=bars), "TEST", "H1", frame, tick_size=0.25, report=report
    )(0)

    assert out.tolist() == [744.25, 754.25, 744.25, 750.25, 748.25]
    assert times.tolist() == [t * 1000 for t in deal_times]
    assert report.rescaled == {START}
    assert report.substituted == set()
    assert report.resolutions == {}


def test_deals_of_a_proportionally_adjusted_series_land_on_its_bars_and_between_them():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # The bar rounds each adjusted price separately, so neither one factor nor one offset reproduces it.
    deal_times = [ms(0), ms(10), ms(20), ms(30), ms(40)]
    prices = [100.0, 104.0, 108.0, 99.0, 102.0]
    bars = [minute_bar("2024-01-02 10:00", 110.09, 118.84, 109.09, 112.34, 5)]
    frame = pd.DataFrame({"open": [110.09], "high": [118.84], "low": [109.09], "close": [112.34]}, index=[START])
    report = ReplayReport()

    _, out = MarketTickReplay(
        TickProvider(prices, times=deal_times, bars=bars),
        "TEST",
        "H1",
        frame,
        tick_size=0.25,
        report=report,
        price_digits=2,
    )(0)

    # 104 lies a third of the way from the close (102 -> 112.34) to the high (108 -> 118.84).
    assert out.tolist() == [110.09, 114.51, 118.84, 109.09, 112.34]
    assert report.rescaled == {START}
    assert report.resolutions == {}


def test_deals_that_carry_the_volume_of_a_bar_they_do_not_trace_are_substituted():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # The deals span 10 points and the bar 20: no shift or factor near the bar's level explains that.
    deal_times = [ms(0), ms(20), ms(40)]
    bars = [minute_bar("2024-01-02 10:00", 744.25, 764.25, 744.25, 744.25, 3)]
    frame = pd.DataFrame({"open": [744.25], "high": [764.25], "low": [744.25], "close": [744.25]}, index=[START])
    report = ReplayReport()

    _, out = MarketTickReplay(
        TickProvider([100.0, 110.0, 100.0], times=deal_times, bars=bars),
        "TEST",
        "H1",
        frame,
        tick_size=0.25,
        report=report,
    )(0)

    # Close 744.25 >= open 744.25: open, low, high, close.
    assert out.tolist() == [744.25, 744.25, 764.25, 744.25]
    assert report.rescaled == set()
    assert set(report.resolutions.values()) == {"minute"}


def test_a_bar_that_orders_its_prices_against_the_deals_is_substituted():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    # The deals close above their open; the bar, one tick away from a shifted copy, closes below it.
    deal_times = [ms(0), ms(20), ms(40)]
    bars = [minute_bar("2024-01-02 10:00", 744.25, 754.25, 744.0, 744.0, 3)]
    frame = pd.DataFrame({"open": [744.25], "high": [754.25], "low": [744.0], "close": [744.0]}, index=[START])
    report = ReplayReport()

    MarketTickReplay(
        TickProvider([100.0, 110.0, 100.25], times=deal_times, bars=bars),
        "TEST",
        "H1",
        frame,
        tick_size=0.25,
        report=report,
    )(0)

    assert report.rescaled == set()
    assert set(report.resolutions.values()) == {"minute"}


def test_adjusted_deals_are_not_rescaled_without_the_instrument_tick_size():
    from q_backend.backtesting.intrabar import MarketTickReplay, ReplayReport

    deal_times, prices = adjusted_minutes()
    bars = [minute_bar("2024-01-02 10:00", 744.25, 754.25, 744.25, 744.25, 3)]
    frame = pd.DataFrame({"open": [744.25], "high": [754.25], "low": [744.25], "close": [744.25]}, index=[START])
    report = ReplayReport()

    MarketTickReplay(TickProvider(prices, times=deal_times, bars=bars), "TEST", "H1", frame, report=report)(0)

    assert report.rescaled == set()
    assert set(report.resolutions.values()) == {"minute"}


def test_the_note_counts_the_candles_whose_deals_were_rescaled():
    from q_backend.backtesting.intrabar import ReplayReport, intrabar_notes

    report = ReplayReport()
    report.substituted.add(START)
    report.rescaled.update({START, START + pd.Timedelta(hours=1)})

    [note] = intrabar_notes([], report)

    assert note["details"]["candles_rescaled"] == 2


def test_market_replay_reports_each_candle_before_its_history_is_read():
    events = []

    class Recording(TickProvider):
        def get_ohlcv(self, symbol, timeframe, start, end):
            if timeframe == "M1":
                events.append(("read", pd.Timestamp(start).isoformat()))
            return super().get_ohlcv(symbol, timeframe, start, end)

    frame = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [103.0, 103.0], "low": [74.0, 74.0], "close": [102.0, 102.0]},
        index=pd.DatetimeIndex(["2024-01-02 10:00", "2024-01-02 11:00"]),
    )
    replay = MarketTickReplay(
        Recording([100.0, 101.0]),
        "TEST",
        "H1",
        frame,
        on_replay=lambda bar, bars_total, candle_time: events.append(("report", bar, bars_total, candle_time)),
    )

    replay(1)
    replay(1)  # served from the one-candle cache: no second report

    assert events == [
        ("report", 1, 2, pd.Timestamp("2024-01-02 11:00")),
        ("read", pd.Timestamp("2024-01-02 11:00").isoformat()),
    ]


def test_stack_job_reports_replayed_candles_while_it_simulates(monkeypatch):
    from q_backend.api import backtest_jobs
    from q_backend.api.backtest_progress import BacktestProgressReporter
    from q_backend.market_data.models import OHLCV

    class Service(TickProvider):
        def get_symbol_info(self, symbol):
            return {"trade_tick_size": 0.25}

        def get_ohlcv(self, symbol, timeframe, start, end):
            if timeframe == "M1":
                return super().get_ohlcv(symbol, timeframe, start, end)
            return [
                OHLCV(time=pd.Timestamp("2024-01-02 09:00"), open=100, high=100, low=100, close=100, tick_volume=1),
                OHLCV(time=pd.Timestamp("2024-01-02 10:00"), open=100, high=100, low=74, close=74, tick_volume=3),
            ]

    monkeypatch.setattr(backtest_jobs, "build_composite_entry", lambda *args: EntryStrategy(breakeven_trigger_pct=0.02))
    request = backtest_jobs.BacktestJobRequest(
        symbol="TEST",
        timeframe="H1",
        entries=[{"strategy": "MACrossover", "params": {}}],
        point_value=12.5,
        exit_params={"breakeven_trigger_pct": 0.02},
    )
    published = []
    # A clock that advances a full interval per call, so every replay report is published.
    ticks = iter(range(1000))
    reporter = BacktestProgressReporter(
        "run",
        started_at=datetime(2024, 1, 2, 9, tzinfo=timezone.utc),
        publish=published.append,
        clock=lambda: float(next(ticks)),
    )
    backtest_jobs._execute_candle(
        request,
        pd.Timestamp("2024-01-02 09:00"),
        pd.Timestamp("2024-01-02 11:00"),
        Service([100, 99, 74]),
        reporter,
    )

    simulating = [payload for payload in published if payload["phase"] == "simulating"]
    replays = [payload for payload in published if payload["message"].startswith("Replaying ticks")]
    assert simulating[0]["message"] == "Simulating 2 candles"
    assert replays, "the breakeven replay must report at least one candle"
    assert {payload["bars_total"] for payload in replays} == {2}
    assert {payload["progress"] for payload in replays} <= {0.5, 1.0}
    assert replays[-1]["simulated_until"] == pd.Timestamp("2024-01-02 10:00").isoformat()
    assert replays[-1]["candles_replayed"] >= 1


def test_replay_completion_follows_reads_and_is_not_repeated_for_cached_candle():
    from contextlib import contextmanager

    events = []

    @contextmanager
    def observe(kind, start, end):
        events.append(("before", kind))
        yield
        events.append(("after", kind))

    replay = MarketTickReplay(
        TickProvider([100, 101, 103, 74, 102]),
        "TEST",
        "H1",
        replay_frame(),
        read_context=observe,
        on_replay_completed=lambda *args: events.append(("completed", args[0])),
    )
    replay(0)
    replay(0)
    assert events[0] == ("before", "reading_m1")
    assert ("before", "reading_ticks") in events
    assert events[-1] == ("completed", 0)
    assert events.count(("completed", 0)) == 1


def test_failed_replay_never_reports_completion():
    class Down(TickProvider):
        def get_ohlcv(self, *args):
            raise ConnectionError("provider unavailable")

    completed = []
    replay = MarketTickReplay(
        Down([]), "TEST", "H1", replay_frame(), on_replay_completed=lambda *args: completed.append(args)
    )
    with pytest.raises(ConnectionError):
        replay(0)
    assert completed == []
