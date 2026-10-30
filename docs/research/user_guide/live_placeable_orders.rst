=========================
Live-placeable orders
=========================

Orders are decided at a completed candle and become effective on later trades.
Resting entries, OCO groups, timed exits and protective levels can be placed
before their fills. Pass a tick store to backtest these orders.

Oco groups
----------

``TradeOrder.oco(order, order, ...)`` takes two or more resting entries (``buy_stop``,
``buy_limit``, ``sell_stop`` or ``sell_limit``), each with its own side, level,
protective levels, reason and expiry. Return the group from ``entry_strategy`` as you
would a single order.

* The first member the ticks fill opens the position and the others are cancelled
  at that trade.
* Members filling on the same trade resolve to the one listed first.
* A group replaces any working entry, as a single resting entry does.
* A group is valid from ``entry_strategy`` only and cannot be nested.

.. code-block:: python

   def entry_strategy(self, frame, positions=()):
       if positions:
           return None
       high, low = frame["high"].iat[-1], frame["low"].iat[-1]
       return TradeOrder.oco(
           TradeOrder.buy_stop(high, stop_loss=low, expire_bars=1, reason="breakout long"),
           TradeOrder.sell_stop(low, stop_loss=high, expire_bars=1, reason="breakout short"),
       )

Timed exits
-----------

``TradeOrder.close(at=...)`` closes the open position at the first trade at or after
the instant, at that trade's price. A timed exit with no position open does nothing.

* A ``datetime.time`` is a time of day, read in the frame's timezone. It means its
  next occurrence after the deciding bar, so a time already passed means the next day.
* A ``datetime`` (or ``pandas.Timestamp``) is an instant. A timezone-aware one is
  converted to the frame's timezone; a naive one is read as the frame's wall clock.
* If the bar's first trade is at or after the instant, the exit fills at that bar's
  open.
* A later ``close(at=...)`` replaces the working one. The exit is dropped once the
  position closes.
* ``close(at=...)`` takes no ``price``, and carries a ``reason`` as any close does.

Forward deployment on MT5
-------------------------

A deployment on an MT5 demo account sends each kind to the broker, which holds it
across candles. The mapping follows the research order:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Research order
     - Broker action
   * - ``buy()`` or ``sell()`` with ``stop_loss`` or ``take_profit``
     - A market deal carrying the levels. The stop and target are broker-side exits.
   * - ``amend(stop_loss=..., take_profit=...)``
     - New levels on the open position. An amend with one level keeps the other.
   * - ``buy_stop``, ``buy_limit``, ``sell_stop``, ``sell_limit``
     - A pending order that rests until it fills, expires or is removed. Its expiry is
       the earlier of ``expire_bars`` (counted from the deciding candle's close) and
       ``expire_at``.
   * - A new resting entry while one is working
     - The working order is removed first, then the new one is placed. If the removal is
       refused, nothing new is placed.
   * - ``cancel()``
     - Removal of the working entry.
   * - ``TradeOrder.oco(...)``
     - One pending order per leg under one group. The first leg to fill removes its
       siblings. If two legs fill before a sibling can be removed, the excess is closed at
       market, a risk event is recorded and the deployment pauses.
   * - ``close(at=...)``
     - The position is closed at market by the worker at the first poll at or after the
       instant. Timed exits act at poll granularity, not per tick.

Fills, expiries and cancellations of resting orders are reconciled with the broker
before each decision and between candles. A stop or target that the broker triggers
appears in the ledger as an exit with reason ``stop_loss`` or ``take_profit``. A
working entry is removed when the deployment pauses, is stopped, the kill switch is
on, the deployment flattens, or the day-trade window closes.

Not yet deployable: exit rules in ``exit_params`` (trailing, breakeven, fixed or ATR stops,
targets and time exits). The forward path does not apply them yet, so a backtest that enables one
is refused at deployment rather than run without it.

Expiry and latency
------------------

``expire_at=`` on ``buy_stop``, ``buy_limit``, ``sell_stop`` and ``sell_limit`` cancels
the order at that instant if it has not filled. It is read as for timed exits. A
resting order expires at whichever of ``expire_bars`` and ``expire_at`` comes first,
and ``expire_bars`` defaults to 1. For a time limit alone, pass ``expire_bars=None``.

``backtest(..., latency_ms=0)`` sets the placement latency in milliseconds. An order
decided at a bar's close fills only from trades at or after the close plus the
latency. The default of 0 leaves every fill unchanged, and a nonzero value is recorded
in the run's ``config``.

Deployment
----------

``oco``, ``timed_exit`` and ``expiring_entry`` appear in a run's ``order_kinds``.
These orders can be deployed to an MT5 demo account. Paper deployments support only
market entries and closes. Tick hooks and historical runs with priced order kinds
are refused. See :doc:`strategy_files` for deployment requirements.
