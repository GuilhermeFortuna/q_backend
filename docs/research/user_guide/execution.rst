===============
Execution model
===============

Start with the fill time: a strategy decision and an executed trade are separate
steps. Market orders fill at the next open; resting orders and protective exits
are resolved by tick replay.

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
   * - Entry with ``stop_loss`` or ``take_profit``
     - Levels fixed when the order is created
     - Entry as above; protective exit resolved from ticks

For example, an unpriced buy decided after the 09:00 five-minute candle fills
at the 09:05 candle's open. Its fill can differ from the close used by the signal.
Exit hooks run before entry hooks. A close and an opposite entry returned on the
same candle can reverse the position at the next open.

Resting entries and timed exits
-------------------------------

Stop and limit entries, OCO groups, timed exits and expiry are described in
:doc:`live_placeable_orders`. These orders are decided at a candle's close and
become effective on later trades.

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

Reading ticks in decision hooks
-------------------------------

An entry or exit hook can declare a keyword-only ``ticks`` parameter to inspect
the completed candle's trades. The hook still runs at the close of the candle;
its order becomes effective on later trades. Use protective levels or a timed
exit when an exit must be held before the next candle.

Session and end-of-run closes
-----------------------------

By default, positions can carry across sessions and remain open at the end of
the data. ``force_close_at_end=True`` closes them at the final candle's close.
With ``day_trade=True``, entry windows and session-close times apply; see
:func:`~q_backend.research.backtest` for the time parameters.
