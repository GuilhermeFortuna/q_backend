import numpy as np
from numba import njit

from q_backend.backtesting.tick.orders import (
    ExitReason,
    SIZING_FIXED_QUANTITY,
    SIZING_FIXED_SAFETY_MARGIN,
)

_EXIT_STOP_LOSS = int(ExitReason.STOP_LOSS)
_EXIT_TAKE_PROFIT = int(ExitReason.TAKE_PROFIT)
_EXIT_SIGNAL = int(ExitReason.SIGNAL)
_EXIT_END_OF_DAY = int(ExitReason.END_OF_DAY)


@njit(cache=True)
def _compute_quantity(
    capital: float,
    sizing_mode: int,
    sizing_a: float,
    sizing_b: float,
    sizing_c: float,
) -> float:
    if sizing_mode == SIZING_FIXED_QUANTITY:
        return sizing_a

    qty = int(capital / sizing_a)
    if sizing_c > 0.0:
        max_c = int(sizing_c)
        if qty > max_c:
            qty = max_c

    min_c = int(sizing_b)
    if qty < min_c:
        if min_c > 0 and qty == 0:
            qty = min_c
        else:
            return 0.0

    if qty <= 0:
        return 0.0
    return float(qty)


@njit(cache=True)
def _simulate_njit(
    bid: np.ndarray,
    ask: np.ndarray,
    direction: np.ndarray,
    sl_points: np.ndarray,
    tp_points: np.ndarray,
    initial_capital: float,
    point_value: float,
    sizing_mode: int,
    sizing_a: float,
    sizing_b: float,
    sizing_c: float,
) -> tuple:
    n = len(bid)
    entry_idx = np.empty(n, dtype=np.int64)
    exit_idx = np.empty(n, dtype=np.int64)
    entry_price = np.empty(n, dtype=np.float64)
    exit_price = np.empty(n, dtype=np.float64)
    trade_direction = np.empty(n, dtype=np.int8)
    quantity = np.empty(n, dtype=np.float64)
    exit_reason = np.empty(n, dtype=np.int32)

    capital = initial_capital
    position_dir = 0
    pos_entry_idx = 0
    pos_entry_price = 0.0
    pos_quantity = 0.0
    sl_price = 0.0
    tp_price = 0.0
    has_sl = False
    has_tp = False
    trade_count = 0
    block_reentry = False

    for i in range(n):
        if block_reentry:
            block_reentry = False

        if position_dir == 0:
            sig = direction[i]
            if sig != 0:
                qty = _compute_quantity(
                    capital, sizing_mode, sizing_a, sizing_b, sizing_c
                )
                if qty > 0.0:
                    if sig > 0:
                        position_dir = 1
                        pos_entry_price = ask[i]
                    else:
                        position_dir = -1
                        pos_entry_price = bid[i]

                    pos_entry_idx = i
                    pos_quantity = qty

                    sl_pts = sl_points[i]
                    tp_pts = tp_points[i]
                    has_sl = not np.isnan(sl_pts)
                    has_tp = not np.isnan(tp_pts)

                    if has_sl:
                        if position_dir > 0:
                            sl_price = pos_entry_price - sl_pts
                        else:
                            sl_price = pos_entry_price + sl_pts
                    if has_tp:
                        if position_dir > 0:
                            tp_price = pos_entry_price + tp_pts
                        else:
                            tp_price = pos_entry_price - tp_pts
        else:
            exited = False
            reason = 0
            fill_price = 0.0

            if position_dir > 0:
                fill_price = bid[i]
                if has_sl and bid[i] <= sl_price:
                    reason = _EXIT_STOP_LOSS
                    exited = True
                elif has_tp and bid[i] >= tp_price:
                    reason = _EXIT_TAKE_PROFIT
                    exited = True
                elif direction[i] < 0:
                    reason = _EXIT_SIGNAL
                    exited = True
            else:
                fill_price = ask[i]
                if has_sl and ask[i] >= sl_price:
                    reason = _EXIT_STOP_LOSS
                    exited = True
                elif has_tp and ask[i] <= tp_price:
                    reason = _EXIT_TAKE_PROFIT
                    exited = True
                elif direction[i] > 0:
                    reason = _EXIT_SIGNAL
                    exited = True

            if exited:
                if position_dir > 0:
                    pnl = (
                        (fill_price - pos_entry_price)
                        * pos_quantity
                        * point_value
                    )
                else:
                    pnl = (
                        (pos_entry_price - fill_price)
                        * pos_quantity
                        * point_value
                    )

                capital += pnl

                entry_idx[trade_count] = pos_entry_idx
                exit_idx[trade_count] = i
                entry_price[trade_count] = pos_entry_price
                exit_price[trade_count] = fill_price
                trade_direction[trade_count] = position_dir
                quantity[trade_count] = pos_quantity
                exit_reason[trade_count] = reason
                trade_count += 1

                position_dir = 0
                block_reentry = True

    if position_dir != 0:
        if position_dir > 0:
            fill_price = bid[n - 1]
            pnl = (fill_price - pos_entry_price) * pos_quantity * point_value
        else:
            fill_price = ask[n - 1]
            pnl = (pos_entry_price - fill_price) * pos_quantity * point_value

        capital += pnl

        entry_idx[trade_count] = pos_entry_idx
        exit_idx[trade_count] = n - 1
        entry_price[trade_count] = pos_entry_price
        exit_price[trade_count] = fill_price
        trade_direction[trade_count] = position_dir
        quantity[trade_count] = pos_quantity
        exit_reason[trade_count] = _EXIT_END_OF_DAY
        trade_count += 1

    return (
        entry_idx,
        exit_idx,
        entry_price,
        exit_price,
        trade_direction,
        quantity,
        exit_reason,
        trade_count,
        capital,
    )


def simulate(
    bid: np.ndarray,
    ask: np.ndarray,
    direction: np.ndarray,
    sl_points: np.ndarray,
    tp_points: np.ndarray,
    initial_capital: float,
    point_value: float,
    sizing_mode: int,
    sizing_a: float,
    sizing_b: float,
    sizing_c: float,
) -> tuple[np.ndarray, ...]:
    """
    Single-position intrabar simulation.

    Returns parallel arrays (one row per closed trade) plus trailing metadata:
      entry_idx, exit_idx, entry_price, exit_price, direction (+1/-1),
      quantity, exit_reason (ExitReason int codes), trade_count, final_capital.
    """
    return _simulate_njit(
        bid,
        ask,
        direction,
        sl_points,
        tp_points,
        initial_capital,
        point_value,
        sizing_mode,
        sizing_a,
        sizing_b,
        sizing_c,
    )
