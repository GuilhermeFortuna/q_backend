import numpy as np

from q_backend.backtesting.tick.kernel import simulate
from q_backend.backtesting.tick.orders import (
    ExitReason,
    SIZING_FIXED_QUANTITY,
    SIZING_FIXED_SAFETY_MARGIN,
)


def _run(
    bid,
    ask,
    direction,
    sl=None,
    tp=None,
    initial_capital=100000.0,
    point_value=1.0,
    sizing_mode=SIZING_FIXED_QUANTITY,
    sizing_a=1.0,
    sizing_b=0.0,
    sizing_c=0.0,
):
    n = len(bid)
    sl_arr = np.full(n, np.nan) if sl is None else np.asarray(sl, dtype=np.float64)
    tp_arr = np.full(n, np.nan) if tp is None else np.asarray(tp, dtype=np.float64)
    return simulate(
        np.asarray(bid, dtype=np.float64),
        np.asarray(ask, dtype=np.float64),
        np.asarray(direction, dtype=np.int8),
        sl_arr,
        tp_arr,
        initial_capital,
        point_value,
        sizing_mode,
        sizing_a,
        sizing_b,
        sizing_c,
    )


def _unpack(result):
    (
        entry_idx,
        exit_idx,
        entry_price,
        exit_price,
        directions,
        quantities,
        exit_reason,
        trade_count,
        final_capital,
    ) = result
    return {
        "entry_idx": entry_idx[:trade_count],
        "exit_idx": exit_idx[:trade_count],
        "entry_price": entry_price[:trade_count],
        "exit_price": exit_price[:trade_count],
        "direction": directions[:trade_count],
        "quantity": quantities[:trade_count],
        "exit_reason": exit_reason[:trade_count],
        "trade_count": trade_count,
        "final_capital": final_capital,
    }


def test_long_stop_loss_before_take_profit():
    bid = np.array([100.0, 97.0, 104.0])
    ask = np.array([100.0, 97.0, 104.0])
    direction = np.array([1, 0, 0], dtype=np.int8)
    sl = np.array([2.0, 2.0, 2.0])
    tp = np.array([3.0, 3.0, 3.0])

    out = _unpack(_run(bid, ask, direction, sl=sl, tp=tp))

    assert out["trade_count"] == 1
    assert out["exit_reason"][0] == ExitReason.STOP_LOSS
    assert out["exit_idx"][0] == 1
    assert out["exit_price"][0] == 97.0


def test_long_take_profit_when_sl_not_hit():
    bid = np.array([100.0, 103.0])
    ask = np.array([100.0, 103.0])
    direction = np.array([1, 0], dtype=np.int8)
    sl = np.array([2.0, 2.0])
    tp = np.array([3.0, 3.0])

    out = _unpack(_run(bid, ask, direction, sl=sl, tp=tp))

    assert out["trade_count"] == 1
    assert out["exit_reason"][0] == ExitReason.TAKE_PROFIT
    assert out["exit_price"][0] == 103.0


def test_long_entry_ask_exit_bid_spread_loss():
    bid = np.array([100.0, 100.0])
    ask = np.array([101.0, 101.0])
    direction = np.array([1, -1], dtype=np.int8)

    out = _unpack(_run(bid, ask, direction, point_value=1.0))

    assert out["trade_count"] == 1
    assert out["entry_price"][0] == 101.0
    assert out["exit_price"][0] == 100.0
    assert out["exit_reason"][0] == ExitReason.SIGNAL
    assert out["final_capital"] == 100000.0 - 1.0


def test_short_entry_bid_exit_ask_spread_loss():
    bid = np.array([100.0, 100.0])
    ask = np.array([101.0, 101.0])
    direction = np.array([-1, 1], dtype=np.int8)

    out = _unpack(_run(bid, ask, direction))

    assert out["entry_price"][0] == 100.0
    assert out["exit_price"][0] == 101.0
    assert out["final_capital"] == 100000.0 - 1.0


def test_opposite_signal_exit():
    bid = np.array([50.0, 50.0, 50.0])
    ask = np.array([50.0, 50.0, 50.0])
    direction = np.array([1, 0, -1], dtype=np.int8)

    out = _unpack(_run(bid, ask, direction))

    assert out["trade_count"] == 1
    assert out["exit_reason"][0] == ExitReason.SIGNAL
    assert out["exit_idx"][0] == 2


def test_end_of_array_force_close():
    bid = np.array([10.0, 11.0])
    ask = np.array([10.0, 11.0])
    direction = np.array([1, 0], dtype=np.int8)

    out = _unpack(_run(bid, ask, direction))

    assert out["trade_count"] == 1
    assert out["exit_reason"][0] == ExitReason.END_OF_DAY
    assert out["exit_idx"][0] == 1


def test_no_reentry_on_same_tick_after_exit():
    bid = np.array([10.0, 10.0, 10.0])
    ask = np.array([10.0, 10.0, 10.0])
    direction = np.array([1, -1, 0], dtype=np.int8)

    out = _unpack(_run(bid, ask, direction))

    assert out["trade_count"] == 1
    assert out["exit_idx"][0] == 1


def test_fixed_quantity_sizing():
    out = _unpack(
        _run(
            [100.0, 100.0],
            [100.0, 100.0],
            [1, -1],
            sizing_a=3.0,
        )
    )
    assert out["quantity"][0] == 3.0
    assert out["final_capital"] == 100000.0


def test_fixed_safety_margin_sizing_and_compounding():
    bid = np.array([10.0, 10.0, 20.0, 20.0])
    ask = np.array([10.0, 10.0, 20.0, 20.0])
    direction = np.array([1, -1, 1, -1], dtype=np.int8)

    out = _unpack(
        _run(
            bid,
            ask,
            direction,
            initial_capital=10000.0,
            point_value=1.0,
            sizing_mode=SIZING_FIXED_SAFETY_MARGIN,
            sizing_a=5000.0,
            sizing_b=1.0,
            sizing_c=0.0,
        )
    )

    assert out["trade_count"] == 2
    assert out["quantity"][0] == 2.0
    # First trade: long 10->10 flat pnl; second sizes from same capital -> still 2
    assert out["quantity"][1] == 2.0
