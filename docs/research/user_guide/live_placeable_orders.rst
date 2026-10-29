=========================
Live-placeable orders
=========================

A research strategy should be deployable as written. A same-candle priced order
(``price=``) cannot be, because the hook places it after seeing the candle, and a
broker only holds orders placed before the candle. Priced entries and closes still
backtest exactly as before, but they now emit ``PricedOrderDeprecationWarning`` and
are removed in a later release. This guide maps each priced pattern to its
replacement, states when the two are identical, and lists the known differences.

The replacements are orders a broker holds across candles:

* ``TradeOrder.oco(...)``: a one-cancels-the-other group of resting entries.
* ``TradeOrder.close(at=...)``: a timed exit.
* ``expire_at=``: a resting entry's expiry instant, besides ``expire_bars``.
* ``backtest(..., latency_ms=...)``: the placement latency.

Migration table
---------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Priced pattern
     - Replacement
     - Identical when
   * - Priced entry at a level fixed before the candle
     - Resting stop or limit placed at the previous close, with ``expire_bars=1``
     - Always
   * - Priced entry list (a bracket)
     - ``TradeOrder.oco(...)`` of resting entries placed at the previous close
     - The levels are fixed before the candle
   * - Priced stop close at a level fixed before the candle
     - ``stop_loss`` set or moved by ``amend`` at the previous close
     - The stop is first set at the entry bar's close. Fills and times match; the exit label differs (see below)
   * - Priced close at the bar's open once a time has passed
     - ``TradeOrder.close(at=time)``
     - Always
   * - A condition that reads the candle's close, high or low and then prices a fill inside that candle
     - None
     - Never. The fill uses information from after the fill; decide it on the previous candle instead

Each replacement is decided at a bar's close, so it works from the next bar on. A
priced order fills within the candle that decided it. That single difference is what
the table's "identical when" column is about.

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
  open. This is how a priced close at the open after a time is reproduced.
* A later ``close(at=...)`` replaces the working one. The exit is dropped once the
  position closes.
* ``close(at=...)`` takes no ``price``, and carries a ``reason`` as any close does.

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

Known differences
-----------------

Two patterns are not identical when ported this way. ``compare_results`` reports
them as documented.

**Strictly beyond.** A legacy hook that enters only when the high is strictly above
the previous high never enters on an exact touch. Its port, a stop at the previous
high, fills on that touch, because a stop fills at the first trade at or beyond its
level. The port has one extra trade, on the candle where the high first equals the
previous high. ``compare_results`` reports it as a one-sided trade on the right.

**Several stops.** A hook that returns several priced stop closes exits at whichever
level the ticks reach first. A port keeps one stop-loss level, so it may exit at a
different price, or not at all. ``compare_results`` reports the differing pair, with
the first field that differs.

**Exit label of a stop-out.** When a protective stop closes the trade, the engine
labels the exit ``STOP_LOSS``. A priced close carries the reason it was given (or
``SIGNAL``). The fills, times and PnL of the stop row are identical; only the exit
label differs.

Comparing runs
--------------

Run the original and the port on the same bars and ticks, then compare them:

.. code-block:: python

   from q_backend.research import compare_results

   report = compare_results(original, ported)
   print(report)          # matched, differing, one-sided trades; PnL, profit factor, drawdown
   assert report.identical

``compare_results`` pairs trades by entry time. For a pair, it reports the first of
these fields that differs: side, entry time, entry price, exit time, exit price,
quantity, entry reason, exit reason and PnL. Prices, quantity and PnL can differ by
up to ``tolerance`` (``compare_results(a, b, tolerance=0.01)``). ``report.identical``
is true only when every trade matches and no trade is one-sided.

Deployment
----------

``oco``, ``timed_exit`` and ``expiring_entry`` appear in a run's ``order_kinds``.
Until live submission of these orders lands, the deployment check refuses any run
whose ``order_kinds`` is not limited to ``market_entry`` and ``market_close``, so
these runs cannot be deployed.
