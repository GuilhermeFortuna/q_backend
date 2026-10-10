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

.. important::

   The hook sees the whole completed candle. Checking its close and then asking
   for an earlier same-candle fill can use information unavailable at that fill.
   The engine checks the price range, but cannot prove your strategy's causality.
   Same-candle priced fills are a research model; live deployment does not
   reproduce them.

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
