import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from q_backend.backtesting.models import SignalAction, Trade
from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.exit_rules.registry import (
    EXIT_RULES,
    all_param_specs,
    enabled_rules,
    required_columns,
)
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.technical_indicators import compute_atr, compute_donchian_channels
from q_backend.backtesting.models import Signal
from typing import List


def _long_trade(entry_price: float = 100.0) -> Trade:
    return Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_price=entry_price,
    )


def _short_trade(entry_price: float = 100.0) -> Trade:
    return Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.SELL,
        quantity=1.0,
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_price=entry_price,
    )


def _run_bars(exit_strat: ExitStrategy, trade: Trade, bars: list[dict]) -> int | None:
    """Return index of first bar that triggers an exit, or None."""
    for idx, bar in enumerate(bars):
        if exit_strat.check_exits([trade], pd.Series(bar)):
            return idx
    return None


GOLDEN_OHLC = pd.DataFrame(
    {
        "open": [100.0, 101.0, 103.0, 102.0, 98.0, 97.5],
        "high": [101.0, 103.0, 106.0, 104.0, 99.0, 98.0],
        "low": [99.5, 100.5, 103.5, 102.5, 96.5, 97.0],
        "close": [100.5, 102.5, 105.0, 103.0, 97.0, 97.5],
    },
    index=pd.date_range("2024-01-01", periods=6, freq="h", tz=timezone.utc),
)


class SingleEntryStrategy(TradingStrategy):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._entered = False

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        return data

    def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
        if not self._entered:
            self._entered = True
            return [Signal(symbol="TEST", action=SignalAction.BUY)]
        return []

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        return []

    def get_chart_indicators(self):
        return []


def _run_golden_backtest(exit_params: dict) -> list[tuple[int, float | None]]:
    strategy = SingleEntryStrategy(**exit_params)
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)
    registry = engine.run(GOLDEN_OHLC.copy(), parallel_mode=ParallelMode.SEQUENTIAL)
    closed = registry.get_closed_trades()
    assert len(closed) == 1
    trade = closed[0]
    exit_idx = GOLDEN_OHLC.index.get_loc(trade.exit_time)
    return [(exit_idx, trade.exit_price)]


def test_registry_lists_all_exit_rules():
    assert len(EXIT_RULES) == 11
    assert [rule.id for rule in EXIT_RULES] == [
        "fixed_sl",
        "atr_sl",
        "fixed_tp",
        "atr_tp",
        "trailing",
        "chandelier",
        "breakeven",
        "psar",
        "profit_target_ratchet",
        "time_stop",
        "donchian_stop",
    ]


def test_all_param_specs_includes_specialized_rules():
    specs = all_param_specs()
    assert len(specs) == 15
    assert all(spec.exit_group is not None for spec in specs)


def test_enabled_rules_respects_zero_defaults():
    assert enabled_rules({}) == []
    params = {"stop_loss_pct": 0.02, "trailing_stop_pct": 0.01}
    enabled_ids = [rule.id for rule in enabled_rules(params)]
    assert enabled_ids == ["fixed_sl", "trailing"]


def test_required_columns_only_for_atr_rules():
    assert required_columns({"stop_loss_pct": 0.02}) == []
    assert required_columns({"stop_loss_atr": 1.5, "atr_period": 14}) == ["atr_14"]
    assert required_columns(
        {"stop_loss_atr": 1.5, "take_profit_atr": 2.0, "atr_period": 21}
    ) == ["atr_21"]


def test_golden_fixed_stop_loss_long():
    result = _run_golden_backtest({"stop_loss_pct": 0.02})
    assert result == [(5, GOLDEN_OHLC.iloc[5]["open"])]


def test_golden_fixed_take_profit_long():
    result = _run_golden_backtest({"take_profit_pct": 0.04})
    assert result == [(3, GOLDEN_OHLC.iloc[3]["open"])]


def test_golden_trailing_stop_long():
    result = _run_golden_backtest({"trailing_stop_pct": 0.02})
    assert result == [(2, GOLDEN_OHLC.iloc[2]["open"])]


def test_coordinator_first_trigger_wins_same_bar():
    exit_strat = ExitStrategy(stop_loss_pct=0.02, take_profit_pct=0.50)
    trade = _long_trade()
    current_data = pd.Series({"close": 100.0, "high": 120.0, "low": 97.0})
    exits = exit_strat.check_exits([trade], current_data)
    assert len(exits) == 1
    assert exits[0].action == SignalAction.CLOSE


def test_coordinator_multiple_rules_stack_without_conflict():
    exit_strat = ExitStrategy(stop_loss_pct=0.02, trailing_stop_pct=0.02)
    trade = _long_trade()
    current_data = pd.Series({"close": 101.0, "high": 102.0, "low": 100.5})
    exits = exit_strat.check_exits([trade], current_data)
    assert len(exits) == 0
    assert exit_strat._extreme_prices[trade.id] == 102.0


def test_atr_warmup_skips_exit_while_nan():
    exit_strat = ExitStrategy(stop_loss_atr=1.5, atr_period=14)
    trade = _long_trade()
    nan_data = pd.Series({"close": 100.0, "high": 101.0, "low": 96.0, "atr_14": float("nan")})
    assert exit_strat.check_exits([trade], nan_data) == []
    ready_data = pd.Series({"close": 100.0, "high": 101.0, "low": 96.0, "atr_14": 2.0})
    assert len(exit_strat.check_exits([trade], ready_data)) == 1


def test_engine_precomputes_only_required_columns():
    strategy = SingleEntryStrategy(stop_loss_pct=0.02)
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)

    with patch(
        "q_backend.backtesting.technical_indicators.compute_atr",
        side_effect=AssertionError("ATR should not be computed for percent-only exits"),
    ):
        engine.run(GOLDEN_OHLC.copy(), parallel_mode=ParallelMode.SEQUENTIAL)


def test_engine_precomputes_atr_when_atr_exit_enabled():
    strategy = SingleEntryStrategy(stop_loss_atr=1.5, atr_period=14)
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)

    with patch(
        "q_backend.backtesting.technical_indicators.compute_atr",
        wraps=compute_atr,
    ) as compute_atr_mock:
        engine.run(GOLDEN_OHLC.copy(), parallel_mode=ParallelMode.SEQUENTIAL)
        compute_atr_mock.assert_called_once()


def test_state_lifecycle_pruned_when_trade_closes():
    exit_strat = ExitStrategy(trailing_stop_pct=0.02)
    trade = _long_trade()
    bar = pd.Series({"close": 101.0, "high": 102.0, "low": 100.5})
    exit_strat.check_exits([trade], bar)
    assert trade.id in exit_strat._state
    exit_strat.check_exits([], bar)
    assert trade.id not in exit_strat._state


def test_chandelier_long_fires_on_pullback():
    exit_strat = ExitStrategy(chandelier_atr_mult=1.5, atr_period=14)
    trade = _long_trade()
    bars = [
        {"close": 109.0, "high": 110.0, "low": 108.0, "atr_14": 2.0},
        {"close": 111.0, "high": 112.0, "low": 110.0, "atr_14": 2.0},
        {"close": 106.0, "high": 111.0, "low": 105.0, "atr_14": 2.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2


def test_chandelier_short_mirror():
    exit_strat = ExitStrategy(chandelier_atr_mult=1.5, atr_period=14)
    trade = _short_trade()
    bars = [
        {"close": 94.0, "high": 95.0, "low": 93.0, "atr_14": 2.0},
        {"close": 92.0, "high": 93.0, "low": 91.0, "atr_14": 2.0},
        {"close": 93.0, "high": 95.0, "low": 92.0, "atr_14": 2.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2


def test_chandelier_warmup_skips_nan_atr():
    exit_strat = ExitStrategy(chandelier_atr_mult=2.0, atr_period=14)
    trade = _long_trade()
    bar = {"close": 100.0, "high": 110.0, "low": 95.0, "atr_14": float("nan")}
    assert _run_bars(exit_strat, trade, [bar]) is None


def test_breakeven_long_arms_and_exits_on_dip():
    exit_strat = ExitStrategy(breakeven_trigger_pct=0.02, breakeven_offset_pct=0.001)
    trade = _long_trade()
    bars = [
        {"close": 101.0, "high": 101.0, "low": 100.0},
        {"close": 103.0, "high": 103.0, "low": 102.0},
        {"close": 100.05, "high": 100.5, "low": 100.05},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2


def test_breakeven_long_never_arms_without_trigger():
    exit_strat = ExitStrategy(breakeven_trigger_pct=0.05, breakeven_offset_pct=0.001)
    trade = _long_trade()
    bars = [
        {"close": 101.0, "high": 101.0, "low": 99.0},
        {"close": 100.0, "high": 100.5, "low": 99.0},
    ]
    assert _run_bars(exit_strat, trade, bars) is None


def test_breakeven_short_mirror():
    exit_strat = ExitStrategy(breakeven_trigger_pct=0.02, breakeven_offset_pct=0.001)
    trade = _short_trade()
    bars = [
        {"close": 99.0, "high": 100.0, "low": 99.0},
        {"close": 97.0, "high": 98.0, "low": 97.0},
        {"close": 99.95, "high": 99.95, "low": 99.5},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2


def test_parabolic_sar_long_hand_computed_series():
    from q_backend.backtesting.exit_rules.parabolic_sar import update_psar_long

    state: dict = {}
    bars = [
        (105.0, 101.0),
        (108.0, 104.0),
        (110.0, 106.0),
        (109.0, 100.0),
    ]
    for high, low in bars:
        update_psar_long(state, high, low, 0.02, 0.02, 0.2, 100.0)

    exit_strat = ExitStrategy(psar_af_start=0.02, psar_af_step=0.02, psar_af_max=0.2)
    trade = _long_trade()
    for idx, (high, low) in enumerate(bars):
        bar = {"close": (high + low) / 2, "high": high, "low": low}
        if exit_strat.check_exits([trade], pd.Series(bar)):
            assert idx == 3
            assert pytest.approx(state["sar"], rel=1e-6) == exit_strat._state[trade.id]["psar"]["sar"]
            return
    pytest.fail("expected PSAR exit on bar 3")


def test_parabolic_sar_short_mirror():
    exit_strat = ExitStrategy(psar_af_start=0.02, psar_af_step=0.02, psar_af_max=0.2)
    trade = _short_trade()
    bars = [
        {"close": 97.0, "high": 99.0, "low": 95.0},
        {"close": 94.0, "high": 97.0, "low": 92.0},
        {"close": 92.0, "high": 96.0, "low": 90.0},
        {"close": 97.0, "high": 100.0, "low": 94.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 3


def test_parabolic_sar_af_caps_at_max():
    from q_backend.backtesting.exit_rules.parabolic_sar import update_psar_long

    state: dict = {}
    for high in range(101, 111):
        update_psar_long(state, float(high), float(high - 1), 0.02, 0.02, 0.08, 100.0)
    assert state["af"] == 0.08


def test_specialized_rules_disabled_parity_with_legacy():
    legacy = _run_golden_backtest({"stop_loss_pct": 0.02})
    with_defaults = _run_golden_backtest(
        {
            "stop_loss_pct": 0.02,
            "chandelier_atr_mult": 0.0,
            "breakeven_trigger_pct": 0.0,
            "psar_af_start": 0.0,
        }
    )
    assert with_defaults == legacy


def test_chandelier_and_atr_stop_first_trigger_wins():
    exit_strat = ExitStrategy(
        chandelier_atr_mult=3.0,
        stop_loss_atr=1.0,
        atr_period=14,
    )
    trade = _long_trade(100.0)
    bar = {"close": 98.0, "high": 99.0, "low": 97.0, "atr_14": 2.0}
    exits = exit_strat.check_exits([trade], pd.Series(bar))
    assert len(exits) == 1
    atr_stop = 100.0 - (1.0 * 2.0)
    assert 97.0 <= atr_stop


def test_profit_target_ratchet_long_arms_trails_and_exits():
    exit_strat = ExitStrategy(target_ratchet_atr=2.0, atr_period=14)
    trade = _long_trade(100.0)
    bars = [
        {"close": 103.0, "high": 104.0, "low": 102.0, "atr_14": 2.0},
        {"close": 107.0, "high": 108.0, "low": 106.0, "atr_14": 2.0},
        {"close": 105.0, "high": 106.0, "low": 103.0, "atr_14": 2.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2
    state = exit_strat._state[trade.id]["profit_target_ratchet"]
    assert state["armed"] is True
    assert state["ratchet"] == 108.0 - (2.0 * 2.0)


def test_profit_target_ratchet_long_never_arms_without_multiple():
    exit_strat = ExitStrategy(target_ratchet_atr=2.0, atr_period=14)
    trade = _long_trade(100.0)
    bars = [
        {"close": 102.0, "high": 103.0, "low": 101.0, "atr_14": 2.0},
        {"close": 103.0, "high": 103.5, "low": 102.0, "atr_14": 2.0},
    ]
    assert _run_bars(exit_strat, trade, bars) is None
    assert "armed" not in exit_strat._state[trade.id]["profit_target_ratchet"]


def test_profit_target_ratchet_short_mirror():
    exit_strat = ExitStrategy(target_ratchet_atr=2.0, atr_period=14)
    trade = _short_trade(100.0)
    bars = [
        {"close": 97.0, "high": 98.0, "low": 96.0, "atr_14": 2.0},
        {"close": 93.0, "high": 94.0, "low": 92.0, "atr_14": 2.0},
        {"close": 95.0, "high": 96.0, "low": 94.0, "atr_14": 2.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2


def test_profit_target_ratchet_warmup_skips_nan_atr():
    exit_strat = ExitStrategy(target_ratchet_atr=2.0, atr_period=14)
    trade = _long_trade()
    bar = {"close": 110.0, "high": 110.0, "low": 109.0, "atr_14": float("nan")}
    assert _run_bars(exit_strat, trade, [bar]) is None


def test_time_stop_closes_on_max_bars():
    exit_strat = ExitStrategy(max_bars_in_trade=3)
    trade = _long_trade()
    bars = [
        {"close": 100.5, "high": 101.0, "low": 100.0},
        {"close": 101.0, "high": 101.5, "low": 100.5},
        {"close": 101.5, "high": 102.0, "low": 101.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 2
    assert exit_strat._state[trade.id]["time_stop"]["bars"] == 3


def test_time_stop_disabled_by_default():
    exit_strat = ExitStrategy(max_bars_in_trade=0)
    trade = _long_trade()
    bars = [{"close": 100.0, "high": 101.0, "low": 99.0}] * 5
    assert _run_bars(exit_strat, trade, bars) is None


def test_donchian_required_columns():
    assert required_columns({"donchian_exit_period": 20}) == [
        "donchian_high_20",
        "donchian_low_20",
    ]


def test_engine_precomputes_donchian_when_donchian_exit_enabled():
    strategy = SingleEntryStrategy(donchian_exit_period=3)
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)

    with patch(
        "q_backend.backtesting.technical_indicators.compute_donchian_channels",
        wraps=compute_donchian_channels,
    ) as compute_donchian_mock:
        engine.run(GOLDEN_OHLC.copy(), parallel_mode=ParallelMode.SEQUENTIAL)
        compute_donchian_mock.assert_called_once()


def test_donchian_long_exits_on_channel_break():
    exit_strat = ExitStrategy(donchian_exit_period=3)
    trade = _long_trade(100.0)
    bars = [
        {"close": 101.0, "high": 102.0, "low": 100.5, "donchian_high_3": 105.0, "donchian_low_3": 99.0},
        {"close": 100.0, "high": 100.5, "low": 98.5, "donchian_high_3": 104.0, "donchian_low_3": 99.5},
    ]
    assert _run_bars(exit_strat, trade, bars) == 1


def test_donchian_short_mirror():
    exit_strat = ExitStrategy(donchian_exit_period=3)
    trade = _short_trade(100.0)
    bars = [
        {"close": 99.0, "high": 99.5, "low": 98.5, "donchian_high_3": 101.0, "donchian_low_3": 95.0},
        {"close": 100.0, "high": 101.5, "low": 99.5, "donchian_high_3": 100.5, "donchian_low_3": 95.0},
    ]
    assert _run_bars(exit_strat, trade, bars) == 1


def test_donchian_warmup_skips_nan_channel():
    exit_strat = ExitStrategy(donchian_exit_period=3)
    trade = _long_trade()
    bar = {
        "close": 100.0,
        "high": 101.0,
        "low": 95.0,
        "donchian_high_3": float("nan"),
        "donchian_low_3": float("nan"),
    }
    assert _run_bars(exit_strat, trade, [bar]) is None


def test_wo63_rules_disabled_parity_with_legacy():
    legacy = _run_golden_backtest({"stop_loss_pct": 0.02})
    with_defaults = _run_golden_backtest(
        {
            "stop_loss_pct": 0.02,
            "target_ratchet_atr": 0.0,
            "max_bars_in_trade": 0,
            "donchian_exit_period": 0,
        }
    )
    assert with_defaults == legacy
