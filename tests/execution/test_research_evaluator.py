"""Tests for ResearchEvaluator: forward evaluation, day-trade session rules, and risk pause."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.models import OrderAction, Trade
from q_backend.execution.domain import SignalAction, StrategyIdentity
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TradeOrder


def _sample_day_bars(
    day: str = "2024-01-02",
    times: list[str] | None = None,
    prices: list[float] | None = None,
) -> pd.DataFrame:
    if times is None:
        times = ["09:00", "09:30", "10:00", "10:30", "15:30", "16:00", "16:30", "17:00"]
    if prices is None:
        prices = [100.0 + i * 2.0 for i in range(len(times))]
    index = pd.DatetimeIndex([f"{day} {t}" for t in times], tz=BRASILIA_TZ)
    close = np.array(prices, dtype=float)
    high = close + 1.0
    low = close - 1.0
    open_ = close - 0.5
    volume = np.full(len(times), 1000.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


class SimpleResearchStrategy(ResearchStrategy):
    parameters = {}

    def compute_indicators(self, frame):
        out = frame.copy()
        out["indicator"] = 1.0
        return out

    def entry_strategy(self, frame, positions=()):
        if not positions and len(frame) == 3:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and len(frame) == 5:
            return TradeOrder.close()
        return None


class RestingOrderStrategy(ResearchStrategy):
    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        return TradeOrder.buy_stop(price=110.0)

    def exit_strategy(self, frame, positions=()):
        return None


class PricedEntryStrategy(ResearchStrategy):
    """A same-candle priced entry: not deployable, unlike resting stop and limit entries."""

    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        return TradeOrder.buy(price=110.0)

    def exit_strategy(self, frame, positions=()):
        return None


class RaisingHookStrategy(ResearchStrategy):
    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        raise RuntimeError("simulated hook failure")

    def exit_strategy(self, frame, positions=()):
        return None


def _make_identity(
    day_trade: bool = True,
    start_time: str = "09:00",
    end_time: str = "16:00",
    close_time: str = "16:30",
) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="simple_research",
        strategy_version=1,
        compiled_config={
            "source_kind": "strategy_version",
            "strategy_params": {},
            "day_trade": day_trade,
            "day_trade_start_time": start_time,
            "day_trade_end_time": end_time,
            "day_trade_close_time": close_time,
        },
        config_hash="cfg-hash",
        symbol="WIN$N",
        timeframe="M30",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )


def test_research_evaluator_market_only_emits_buy_and_close():
    bars = _sample_day_bars()
    identity = _make_identity()
    evaluator = ResearchEvaluator(
        deployment_id="dep-1",
        identity=identity,
        strategy_instance=SimpleResearchStrategy(),
        window_bound=100,
    )

    evaluator.seed_window(bars.iloc[:2])

    # bar index 2 (len(frame) == 3) -> BUY
    buy_result = evaluator.ingest_completed_bar(bars.iloc[2:3])
    assert buy_result.signal_action == SignalAction.BUY
    assert buy_result.requested_quantity == 1.0

    # Simulate fill of buy trade
    evaluator.set_open_trade(
        Trade(
            id="t1",
            order_id="o1",
            symbol="WIN$N",
            action=OrderAction.BUY,
            entry_time=buy_result.bar_close_time,
            entry_price=105.0,
            quantity=1.0,
        )
    )

    # bar index 3 (len(frame) == 4) -> HOLD
    hold_result = evaluator.ingest_completed_bar(bars.iloc[3:4])
    assert hold_result.signal_action == SignalAction.HOLD

    # bar index 4 (len(frame) == 5) -> CLOSE
    close_result = evaluator.ingest_completed_bar(bars.iloc[4:5])
    assert close_result.signal_action == SignalAction.CLOSE


def test_research_evaluator_day_trade_session_enforcement():
    # Day trade start: 10:00, end: 15:00, close: 16:30
    # Open times: 09:00 (close 09:30), 09:30 (close 10:00), 10:00 (close 10:30), 15:00 (close 15:30), 16:00 (close 16:30), 16:30 (close 17:00)
    bars = _sample_day_bars(times=["09:00", "09:30", "10:00", "15:00", "16:00", "16:30"])
    identity = _make_identity(
        day_trade=True,
        start_time="10:00",
        end_time="15:00",
        close_time="16:30",
    )

    class AlwaysBuyStrategy(ResearchStrategy):
        parameters = {}

        def compute_indicators(self, frame):
            return frame

        def entry_strategy(self, frame, positions=()):
            return TradeOrder.buy()

        def exit_strategy(self, frame, positions=()):
            return None

    evaluator = ResearchEvaluator(
        deployment_id="dep-dt",
        identity=identity,
        strategy_instance=AlwaysBuyStrategy(),
        window_bound=100,
    )

    # Bar 0 (close 09:30): before start_time (10:00) -> refused, HOLD
    res0 = evaluator.ingest_completed_bar(bars.iloc[0:1])
    assert res0.signal_action == SignalAction.HOLD

    # Bar 1 (close 10:00): at start_time -> BUY
    res1 = evaluator.ingest_completed_bar(bars.iloc[1:2])
    assert res1.signal_action == SignalAction.BUY

    # Set open trade
    evaluator.set_open_trade(
        Trade(
            id="t1",
            order_id="o1",
            symbol="WIN$N",
            action=OrderAction.BUY,
            entry_time=res1.bar_close_time,
            entry_price=102.0,
            quantity=1.0,
        )
    )

    # Bar 2 (close 10:30): position open, before close_time -> HOLD
    res2 = evaluator.ingest_completed_bar(bars.iloc[2:3])
    assert res2.signal_action == SignalAction.HOLD

    # Bar 3 (close 15:30): after end_time, before close_time -> HOLD
    res3 = evaluator.ingest_completed_bar(bars.iloc[3:4])
    assert res3.signal_action == SignalAction.HOLD

    # Bar 4 (close 16:30): at close_time -> CLOSE with reason day_trade_close
    res4 = evaluator.ingest_completed_bar(bars.iloc[4:5])
    assert res4.signal_action == SignalAction.CLOSE
    assert res4.reason == "day_trade_close"


def test_research_evaluator_removed_priced_entry_pauses_deployment():
    bars = _sample_day_bars()
    identity = _make_identity()
    evaluator = ResearchEvaluator(
        deployment_id="dep-priced",
        identity=identity,
        strategy_instance=PricedEntryStrategy(),
        window_bound=100,
    )

    results = evaluator.ingest_completed_bars(bars.iloc[:1])
    assert len(results) == 1
    res = results[0]
    assert res.signal_action == SignalAction.HOLD
    assert res.pause_requested is True
    assert res.risk_rejection_code == "hook_error"
    assert "priced entries are removed" in res.risk_rejection_message


def test_research_evaluator_raising_hook_pauses_deployment():
    bars = _sample_day_bars()
    identity = _make_identity()
    evaluator = ResearchEvaluator(
        deployment_id="dep-raise",
        identity=identity,
        strategy_instance=RaisingHookStrategy(),
        window_bound=100,
    )

    results = evaluator.ingest_completed_bars(bars.iloc[:1])
    assert len(results) == 1
    res = results[0]
    assert res.signal_action == SignalAction.HOLD
    assert res.pause_requested is True
    assert res.risk_rejection_code == "hook_error"
    assert "simulated hook failure" in res.risk_rejection_message


def test_research_evaluator_recovery_replay_reconstructs_without_emitting():
    bars = _sample_day_bars()
    identity = _make_identity()

    # Uninterrupted evaluator
    evaluator1 = ResearchEvaluator(
        deployment_id="dep-1",
        identity=identity,
        strategy_instance=SimpleResearchStrategy(),
        window_bound=100,
    )
    evaluator1.seed_window(bars.iloc[:2])
    res_b2 = evaluator1.ingest_completed_bar(bars.iloc[2:3])
    evaluator1.set_open_trade(
        Trade(
            id="t1",
            order_id="o1",
            symbol="WIN$N",
            action=OrderAction.BUY,
            entry_time=res_b2.bar_close_time,
            entry_price=105.0,
            quantity=1.0,
        )
    )
    evaluator1.ingest_completed_bar(bars.iloc[3:4])
    res1_b4 = evaluator1.ingest_completed_bar(bars.iloc[4:5])

    # Restarted evaluator with replay recovery up to bar 3
    watermark = evaluator1.last_evaluated_close
    evaluator2 = ResearchEvaluator(
        deployment_id="dep-1",
        identity=identity,
        strategy_instance=SimpleResearchStrategy(),
        window_bound=100,
        last_evaluated_close=res_b2.bar_close_time,
    )
    evaluator2.set_open_trade(
        Trade(
            id="t1",
            order_id="o1",
            symbol="WIN$N",
            action=OrderAction.BUY,
            entry_time=res_b2.bar_close_time,
            entry_price=105.0,
            quantity=1.0,
        )
    )
    replayed = evaluator2.replay_recovery(bars.iloc[:4], end_close=bars.iloc[3:4].index[0].to_pydatetime())
    for r in replayed:
        assert r.replay is True
        assert r.emits_decision is False

    res2_b4 = evaluator2.ingest_completed_bar(bars.iloc[4:5])
    assert res2_b4.signal_action == res1_b4.signal_action
    assert res2_b4.signal_action == SignalAction.CLOSE


def _open_long(evaluator: ResearchEvaluator, entry_time) -> None:
    evaluator.set_open_trade(
        Trade(
            id="t1",
            order_id="o1",
            symbol="WIN$N",
            action=OrderAction.BUY,
            entry_time=entry_time,
            entry_price=105.0,
            quantity=1.0,
        )
    )


class ExitListStrategy(ResearchStrategy):
    """Returns an amendment and a timed close together, as one list."""

    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        return None

    def exit_strategy(self, frame, positions=()):
        if positions:
            return [TradeOrder.amend(stop_loss=99.0), TradeOrder.close(at=dt.time(16, 0), reason="time up")]
        return None


def test_research_evaluator_applies_every_exit_order_of_a_list():
    bars = _sample_day_bars()
    evaluator = ResearchEvaluator(
        deployment_id="dep-list",
        identity=_make_identity(),
        strategy_instance=ExitListStrategy(),
        window_bound=100,
    )
    evaluator.seed_window(bars.iloc[:2])
    _open_long(evaluator, bars.index[1])

    result = evaluator.ingest_completed_bar(bars.iloc[2:3])

    assert result.signal_action == SignalAction.HOLD
    assert result.protect_levels == {"stop_loss": 99.0, "take_profit": None}
    assert result.timed_exit_at == pd.Timestamp("2024-01-02 16:00", tz=BRASILIA_TZ).to_pydatetime()


class CloseAndRearmStrategy(ResearchStrategy):
    """Closes the open position and, on the same bar, re-arms an OCO bracket for the next one."""

    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        return TradeOrder.oco(
            TradeOrder.buy_stop(130.0, reason="bracket buy"),
            TradeOrder.sell_stop(90.0, reason="bracket sell"),
        )

    def exit_strategy(self, frame, positions=()):
        return TradeOrder.close(reason="rotate") if positions else None


def test_research_evaluator_rearms_a_bracket_on_the_bar_its_close_takes_the_position_out():
    bars = _sample_day_bars()
    evaluator = ResearchEvaluator(
        deployment_id="dep-rearm",
        identity=_make_identity(),
        strategy_instance=CloseAndRearmStrategy(),
        window_bound=100,
    )
    evaluator.seed_window(bars.iloc[:2])
    _open_long(evaluator, bars.index[1])

    result = evaluator.ingest_completed_bar(bars.iloc[2:3])

    assert result.signal_action == SignalAction.CLOSE
    assert result.reason == "rotate"
    assert [leg["price"] for leg in result.pending_group] == [130.0, 90.0]
    assert result.requested_quantity == 1.0


def test_research_evaluator_does_not_rearm_on_a_day_trade_close():
    bars = _sample_day_bars()
    evaluator = ResearchEvaluator(
        deployment_id="dep-rearm-eod",
        identity=_make_identity(),
        strategy_instance=CloseAndRearmStrategy(),
        window_bound=100,
    )
    evaluator.seed_window(bars.iloc[:5])
    _open_long(evaluator, bars.index[1])

    # The 16:00 bar closes at 16:30, the day-trade close time.
    result = evaluator.ingest_completed_bar(bars.iloc[5:6])

    assert result.signal_action == SignalAction.CLOSE
    assert result.reason == "day_trade_close"
    assert result.pending_group is None
