===================
Your first backtest
===================

This walkthrough loads bars, builds a small RSI strategy, and reads its results.
Run the snippets in order in the environment from :doc:`install`.
The dates and instrument are examples; choose a range available from your broker.

Load price data
---------------

.. code-block:: python

   from q_backend.research import load_bars

   bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")
   print(bars[["open", "high", "low", "close"]].tail())

Each row is a completed five-minute candle. The index records the candle's
opening time in ``America/Sao_Paulo``. ``WIN$N`` is an unadjusted continuous
future; :doc:`price_series` explains the alternatives.

**Already have data?** Skip the gateway request and use your own DataFrame.
It must have numeric ``open``, ``high``, ``low``, and ``close`` columns and a
sorted, unique, timezone-aware :class:`pandas.DatetimeIndex`.
Use ``tz_localize`` for naive timestamps in their original timezone, or
``tz_convert`` for timestamps that already have a timezone.

Try an indicator
----------------

.. code-block:: python

   from q_backend.research import indicators

   rsi = indicators.rsi(bars["close"], period=14)
   print(rsi.tail())

The returned Series aligns with the input index. Initial values are ``NaN``
while the indicator warms up. The strategy below computes its own RSI column.

Write entry and exit rules
--------------------------

This example buys when RSI crosses back above 30 and sells when it crosses back
below 70. It closes a long when RSI reaches 50, or a short when RSI falls to 50.
It holds at most one position at a time.

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder

   class RSIReversion(ResearchStrategy):
       def __init__(self, period=14):
           self.period = period

       def compute_indicators(self, frame):
           frame = frame.copy()
           frame["rsi"] = indicators.rsi(frame["close"], period=self.period)
           return frame

       def entry_strategy(self, frame, positions=()):
           if positions or len(frame) < 2:
               return None
           previous, current = frame["rsi"].iloc[-2:]
           if previous <= 30 and current > 30:
               return TradeOrder.buy()
           if previous >= 70 and current < 70:
               return TradeOrder.sell()
           return None

       def exit_strategy(self, frame, positions=()):
           if not positions:
               return None
           current = frame["rsi"].iloc[-1]
           position = positions[0]
           if position.side == "long" and current >= 50:
               return TradeOrder.close()
           if position.side == "short" and current <= 50:
               return TradeOrder.close()
           return None

``compute_indicators`` runs once over the full frame. Decision hooks then see
only the history through the current completed candle. ``positions`` contains
filled open positions. Returning ``None`` means no action; comparisons with
warm-up ``NaN`` values do not generate entries in this example.

Run the backtest
----------------

.. code-block:: python

   from q_backend.backtesting.costs import TransactionCostConfig
   from q_backend.research import backtest

   result = backtest(
       bars,
       strategy=RSIReversion(),
       symbol="WIN$N",
       quantity=1,
       point_value=0.20,
       initial_capital=10_000.0,
       costs=TransactionCostConfig(cost_per_contract=1.00),
       force_close_at_end=True,
   )

``point_value`` converts one price point into account currency per contract.
The cost here is an **illustrative R$1.00 per contract per side**; replace it
with your assumptions. ``force_close_at_end=True`` closes any remaining position
at the last candle's close.

For these unpriced orders, a decision at a candle's close fills at the **next
candle's open**. See :doc:`../user_guide/execution` for other fill models.

Read the results
----------------

.. code-block:: python

   print(result.metrics)
   print(result.trades[["side", "entry_price", "exit_price", "pnl"]].head())
   print(result.equity.tail())

``trades`` contains the execution log. ``equity`` tracks realized equity;
it does not mark open positions to market. A strategy may produce no trades
for your chosen range. :doc:`../user_guide/backtesting` explains the result fields.

Next steps
----------

* Add chart series and position-aware rules: :doc:`../user_guide/strategy`.
* Replay stops and targets from stored ticks: :doc:`../user_guide/execution`.
* Publish this result for visual review: :doc:`../user_guide/publishing`.
