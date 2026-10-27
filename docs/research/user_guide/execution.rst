===============
Execution model
===============

Start with the fill time: a strategy decision and an executed trade are separate
steps. The model depends on whether you request an unpriced entry, a priced entry,
or an exit confirmed by tick replay.

When orders fill
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Request
     - Decision
     - Fill
   * - ``TradeOrder.buy()`` or ``sell()``
     - At a completed candle
     - Next candle's open
   * - ``TradeOrder.close()`` in the ordinary exit hook
     - At a completed candle
     - Next candle's open
   * - ``buy(price=...)`` or ``sell(price=...)``
     - On the deciding candle
     - Specified price within that candle's range
   * - Entry with ``stop_loss`` or ``take_profit``
     - Levels fixed when the order is created
     - Entry as above; protective exit resolved from ticks

For example, an unpriced buy decided after the 09:00 five-minute candle fills
at the 09:05 candle's open. Its fill can differ from the close used by the signal.
Exit hooks run before entry hooks. A close and an opposite entry returned on the
same candle can reverse the position at the next open.

Priced entries
--------------

A priced entry fills exactly at ``price`` if it lies between the deciding
candle's low and high. The model does not declare a limit or stop order type.
A price outside the candle's range raises ``ValueError``; it is not queued for
a later candle.

On a candle where a stop, target, breakeven or priced close closed a trade,
a priced entry fills only from a trade at or after that exit. Otherwise it
appears in ``result.rejected_entries`` with reason ``touch_before_exit``.

The candle's ``exit_strategy`` call runs before a priced entry fills, so it
does not see the new position. Once the entry fills and is still open,
``exit_strategy`` is called again on the same candle with that position in
``positions``. A ``TradeOrder.close()`` returned there exits at the next
candle's open, ahead of that candle's stops and targets. This is how to write a
time-based exit known in advance, such as holding for a fixed number of
candles: return the close on the candle before the exit. A priced close or an
amend returned from this second call raises ``ValueError``.

.. important::

   The hook sees the whole completed candle. Checking its close and then asking
   for an earlier same-candle fill can use information unavailable at that fill.
   The engine checks the price range, but cannot prove your strategy's causality.
   Same-candle priced fills are a research model; live deployment does not
   reproduce them.

Order lists
-----------

A hook may return a list of orders when it cannot know which of them the
candle traded first. The engine replays the candle's ticks and fills the first
one the tape reaches:

* ``exit_strategy`` may list any number of priced closes, at most one close
  without a price and at most one amend. The position closes at the priced
  close its ticks reach first, under that close's ``reason``; on a tie the
  earlier listed close wins. A close without a price beside them exits at the
  next open when none is reached.
* ``entry_strategy`` may list priced market entries. The first one the ticks
  touch fills with its own levels and reason; the others are cancelled. After
  a same-candle exit only touches from that exit on count, and if none comes,
  each entry appears in ``result.rejected_entries`` with reason
  ``touch_before_exit``. A list cannot mix priced entries with unpriced,
  stop or limit entries.

``entry_strategy`` runs after the candle's priced closes, so its ``positions``
no longer holds a position they closed, and it can open on the same candle.

A stop and a target that a strategy tracks itself, where one candle can reach
both:

.. code-block:: python

   def exit_strategy(self, frame, positions):
       if not positions:
           return None
       open_, high, low = frame["open"].iat[-1], frame["high"].iat[-1], frame["low"].iat[-1]
       orders = []
       if low <= self.stop:
           orders.append(TradeOrder.close(price=min(open_, self.stop), reason="Stop"))
       if high >= self.target:
           orders.append(TradeOrder.close(price=max(open_, self.target), reason="Target"))
       return orders

A bracket around the previous close, one side cancelling the other:

.. code-block:: python

   def entry_strategy(self, frame, positions):
       high_level, low_level = frame["bracket_high"].iat[-2], frame["bracket_low"].iat[-2]
       orders = []
       if frame["high"].iat[-1] >= high_level:
           orders.append(TradeOrder.buy(price=max(frame["open"].iat[-1], high_level)))
       if frame["low"].iat[-1] <= low_level:
           orders.append(TradeOrder.sell(price=min(frame["open"].iat[-1], low_level)))
       return orders

The tick replay orders the listed prices, but cannot tell whether the strategy
could have known them before the candle. List only levels fixed in advance,
such as stops, targets and brackets.

Stops and targets from ticks
----------------------------

Use bars and ticks from the **same TickStore**, and pass the store as ``ticks=``:

.. code-block:: python

   from q_backend.research import TickStore, backtest

   store = TickStore("WDO$N")
   bars = store.bars("M10", start="2026-09-01", end="2026-09-30")
   # strategy returns orders with stop_loss and/or take_profit.
   result = backtest(
       bars, strategy=strategy, symbol="WDO$N", point_value=10.0, ticks=store,
   )

Sync the sessions first as described in :doc:`tick_store`. Tick replay verifies
that the stored trade prices reproduce the candle's open, high, low, and close.
Missing sessions, empty replay intervals, or mismatched candles fail the run;
there is no fallback fill model.

* A stop triggers at the first trade price **at or beyond** its level and fills
  at that traded price. A gap can produce a worse fill than the stop level.
* A target triggers at the first trade price **strictly beyond** its level and
  fills at the target level. A touch exactly at the target does not fill.
* Tick order resolves which protective level triggers first. Protective exits
  take precedence over custom exits at the same tick.

Levels must lie on the correct side of the actual entry price. Invalid entries
are reported in ``result.rejected_entries``. For an unpriced order, the next
open may make a level invalid even if it was valid at the signal candle.

Custom intrabar exits
---------------------

To evaluate exits before a candle closes, add the keyword-only ``phase``
parameter to your exit hook and supply ``ticks=store``:

.. code-block:: python

   def exit_strategy(self, frame, positions=(), *, phase="bar"):
       ...

.. list-table::
   :header-rows: 1
   :widths: 15 50 35

   * - Phase
     - What the hook sees
     - Meaning of ``TradeOrder.close()``
   * - ``screen``
     - The candle's full range, used to select candidates for replay
     - Request tick replay; this does not execute a close
   * - ``tick``
     - History ending with the candle as observed up to this tick
     - Close at the current trade price
   * - ``bar``
     - Completed candle history
     - Close at the next candle's open

Write each branch for the information available in that phase. The full candle
in ``screen`` selects replay candidates; actual intrabar decisions belong in
``tick``. Tick replay and protective levels are research features; live
strategies decide on completed bars.

Session and end-of-run closes
-----------------------------

By default, positions can carry across sessions and remain open at the end of
the data. ``force_close_at_end=True`` closes them at the final candle's close.
With ``day_trade=True``, entry windows and session-close times apply; see
:func:`~q_backend.research.backtest` for the time parameters.
