"""Actual execution state supplied to research decision hooks."""

from dataclasses import FrozenInstanceError

import pandas as pd
import pytest

from q_backend.research import ResearchPosition, ResearchStrategy, TradeOrder, backtest


@pytest.fixture
def bars():
    index = pd.date_range("2026-10-01 09:00", periods=6, freq="5min", tz="America/Sao_Paulo")
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    return pd.DataFrame(dict(open=prices, high=prices, low=prices, close=prices), index=index)


def test_actual_positions_close_and_reverse(bars):
    seen = []
    computed = []

    class Strategy(ResearchStrategy):
        def compute_indicators(self, frame):
            computed.append(len(frame))
            return frame.assign(indicator=1.0)

        def exit_strategy(self, frame, positions):
            seen.append(("exit", len(frame), positions))
            assert not frame.attrs
            frame["indicator"] = -1.0
            if positions and len(frame) in (2, 4):
                return TradeOrder.close()

        def entry_strategy(self, frame, *, positions):
            seen.append(("entry", len(frame), positions))
            assert frame["indicator"].eq(1.0).all()
            if len(frame) == 1:
                return TradeOrder.buy()
            if len(frame) == 2:
                assert positions[0].side == "long"
                return TradeOrder.sell()

    result = backtest(bars, strategy=Strategy(), symbol="TEST", quantity=2)
    assert computed == [6]
    for exit_call, entry_call in zip(seen[::2], seen[1::2]):
        assert exit_call[1:] == entry_call[1:]
        assert exit_call[2] is entry_call[2]
    assert seen[0][2] == ()
    long = seen[2][2][0]
    assert long == ResearchPosition("TEST", "long", bars.index[1], 101.0, 2.0)
    with pytest.raises(FrozenInstanceError):
        long.side = "short"
    assert seen[4][2][0].side == "short"
    assert seen[4][2][0].entry_time == bars.index[2]
    assert seen[8][2] == ()
    assert result.trades["side"].tolist() == ["long", "short"]
    assert result.trades["exit_price"].tolist() == [102.0, 104.0]
    assert result.data["indicator"].eq(1.0).all()


def test_mixed_signature_and_entry_capacity(bars):
    seen = []

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame):
            return TradeOrder.buy() if len(frame) == 1 else TradeOrder.sell()

        def exit_strategy(self, frame, positions):
            seen.append(positions)

    result = backtest(bars, strategy=Strategy(), symbol="TEST")
    assert len(result.trades) == 1
    assert all(len(positions) == 1 for positions in seen[1:])
    assert seen[1][0] == seen[-1][0]


def test_rule_exit_disappears_after_fill(bars):
    seen = []

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            seen.append(positions)
            return TradeOrder.buy() if len(frame) == 1 else None

    backtest(bars, strategy=Strategy(), symbol="TEST", exit_params={"take_profit_pct": 0.005})
    assert seen[1][0].side == "long"
    assert seen[2][0].side == "long"  # exit request is pending
    assert seen[3] == ()


def test_hook_exception_is_not_retried(bars):
    calls = []
    marker = TypeError("hook marker")

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            calls.append(len(frame))
            raise marker

    with pytest.raises(RuntimeError, match=r"Strategy.entry_strategy at bar") as error:
        backtest(bars, strategy=Strategy(), symbol="TEST")
    assert error.value.__cause__ is marker
    assert calls == [1]


def test_empty_and_repeated_runs(bars):
    calls = []

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            calls.append(positions)
            return TradeOrder.buy() if len(frame) == 1 else None

    strategy = Strategy()
    assert backtest(bars.iloc[:0], strategy=strategy, symbol="TEST").trades.empty
    assert not calls
    for _ in range(2):
        backtest(bars, strategy=strategy, symbol="TEST", force_close_at_end=True)
    assert calls[0] == calls[6] == ()
    assert len(calls) == 12


def test_invalid_signature_rejected_before_indicator_computation(bars):
    class Strategy(ResearchStrategy):
        def compute_indicators(self, frame):
            raise AssertionError("must validate signatures first")

        def entry_strategy(self, frame, positions, required):
            return None

    with pytest.raises(TypeError, match="entry_strategy.*signature"):
        backtest(bars, strategy=Strategy(), symbol="TEST")


@pytest.mark.parametrize("side", ["long", "short"])
def test_direction_dependent_exit(bars, side):
    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            if len(frame) == 1:
                return TradeOrder.buy() if side == "long" else TradeOrder.sell()

        def exit_strategy(self, frame, positions):
            if positions:
                expected_bar = 3 if positions[0].side == "long" else 4
                if len(frame) == expected_bar:
                    return TradeOrder.close()

    result = backtest(bars, strategy=Strategy(), symbol="TEST")
    trade = result.trades.iloc[0]
    assert trade["side"] == side
    assert trade["exit_time"] == bars.index[3 if side == "long" else 4]


def test_positional_only_context_and_frame_attributes(bars):
    calls = []
    bars.attrs["q_research"] = {"end": bars.index[-1]}

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions, /):
            assert not frame.attrs
            assert frame.index[-1] == bars.index[len(frame) - 1]
            calls.append(positions)

    result = backtest(bars, strategy=Strategy(), symbol="TEST")
    assert calls == [()] * len(bars)
    assert bars.attrs["q_research"]["end"] == bars.index[-1]
    assert result.trades.empty


@pytest.mark.parametrize(
    "hook,decision,error",
    [
        ("entry_strategy", TradeOrder.close(), ValueError),
        ("exit_strategy", TradeOrder.buy(), ValueError),
        ("entry_strategy", "buy", TypeError),
        ("exit_strategy", [], TypeError),
    ],
)
def test_invalid_runtime_orders(bars, hook, decision, error):
    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            return None

        def exit_strategy(self, frame, positions):
            return None

    def invalid(self, frame, positions):
        return decision

    setattr(Strategy, hook, invalid)
    with pytest.raises(error, match=hook + " at bar"):
        backtest(bars, strategy=Strategy(), symbol="TEST")


def test_terminal_close_occurs_after_final_snapshot(bars):
    seen = []

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            seen.append(positions)
            return TradeOrder.buy() if len(frame) == 1 else None

    result = backtest(bars, strategy=Strategy(), symbol="TEST", force_close_at_end=True)
    assert len(seen[-1]) == 1
    assert result.trades.iloc[0]["status"] == "closed"
    assert result.trades.iloc[0]["exit_time"] == bars.index[-1]


def test_daily_closure_snapshot_ordering(bars):
    seen = []

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            seen.append(positions)
            return TradeOrder.buy()

    result = backtest(
        bars,
        strategy=Strategy(),
        symbol="TEST",
        day_trade=True,
        day_trade_start_time="09:00",
        day_trade_end_time="09:10",
        day_trade_close_time="09:15",
    )
    assert len(seen) == len(bars)
    assert len(seen[2]) == 1
    assert seen[3:] == [(), (), ()]
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["exit_time"] == bars.index[3]


def test_runtime_and_legacy_numerical_parity(bars):
    from q_backend.backtesting.costs import TransactionCostConfig

    class Legacy(ResearchStrategy):
        def entry_strategy(self, frame):
            if len(frame) == 1:
                return TradeOrder.buy()
            if len(frame) == 3:
                return TradeOrder.sell()

        def exit_strategy(self, frame):
            return TradeOrder.close() if len(frame) == 3 else None

    class Context(Legacy):
        def entry_strategy(self, frame, positions):
            return super().entry_strategy(frame)

    kwargs = dict(
        symbol="TEST",
        quantity=2,
        costs=TransactionCostConfig(cost_per_contract=1.0),
        exit_params={"take_profit_pct": 0.005},
        force_close_at_end=True,
    )
    expected = backtest(bars, strategy=Legacy(), **kwargs)
    actual = backtest(bars, strategy=Context(), **kwargs)
    pd.testing.assert_frame_equal(actual.trades.drop(columns="trade_id"), expected.trades.drop(columns="trade_id"))
    pd.testing.assert_frame_equal(actual.equity, expected.equity)
    assert actual.metrics == expected.metrics


def test_missing_callback_support_has_actionable_error(bars, monkeypatch):
    from q_backend.backtesting import candle_kernel

    def old_kernel(*, entry):
        raise AssertionError("old kernel must not execute")

    monkeypatch.setattr(candle_kernel.engine, "run_candle", old_kernel)

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame, positions):
            return None

    with pytest.raises(ImportError, match="q_core release with candle strategy_callback support"):
        backtest(bars, strategy=Strategy(), symbol="TEST")


@pytest.mark.parametrize("context", [False, True])
def test_hook_timestamp_storage_is_owned(bars, context):
    original_index = bars.index.copy(deep=True)

    def exit_hook(frame):
        backing = frame.index.values.base
        if backing is not None:
            assert backing.size <= len(frame), "future timestamps exposed through index backing storage"
        values = frame.index.values
        values.setflags(write=True)
        values[0] = pd.Timestamp("1999-01-01").to_datetime64()

    def entry_hook(frame):
        assert frame.index[0] == original_index[0]
        return TradeOrder.buy() if len(frame) == 1 else None

    class Strategy(ResearchStrategy):
        def entry_strategy(self, frame):
            return entry_hook(frame)

        def exit_strategy(self, frame):
            return exit_hook(frame)

    if context:

        def contextual_exit(self, frame, positions):
            return exit_hook(frame)

        Strategy.exit_strategy = contextual_exit

    result = backtest(bars, strategy=Strategy(), symbol="TEST")
    pd.testing.assert_index_equal(bars.index, original_index)
    assert result.trades.iloc[0]["entry_time"] == original_index[1]
