===============
Execution Model
===============

Causality & Fill Mechanics
--------------------------

Q Research models realistic exchange mechanics without lookahead bias:

- **Unpriced Market Orders:** Decisions returned on bar :math:`t` fill at the opening price of bar :math:`t+1`.
- **Priced Limit Orders:** An order specifying ``price=...`` fills on the deciding bar :math:`t` if :math:`\text{low}_t \le \text{price} \le \text{high}_t`.
- **Protective Levels:** ``stop_loss`` and ``take_profit`` fixed at order construction are verified against actual intra-candle ticks using :class:`~q_backend.research.TickStore`.

Phase-Aware Intrabar Exits
--------------------------

To evaluate exits inside the bar before it closes, declare the keyword-only parameter ``phase`` in ``exit_strategy``:

.. code-block:: python

   def exit_strategy(self, frame, positions=(), *, phase="bar"):
       ...

The hook is called in three distinct phases:

1. ``phase="screen"``: Evaluated once per candle across the full high/low range. Return :meth:`~q_backend.research.TradeOrder.close` to qualify the candle for tick replay.
2. ``phase="tick"``: Evaluated chronologically for every trade price in the candle. The frame ends with the candle observed so far. Returning close exits at that exact tick price.
3. ``phase="bar"``: The standard closed-bar evaluation. A close fills at the next bar's open.
