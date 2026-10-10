===================
Desktop Publishing
===================

Visualizing Backtests in Q Research
-----------------------------------

Completed research backtests can be published directly to the Q Research desktop application for visual exploration and review:

.. code-block:: python

   result = backtest(bars, strategy=MyStrategy(), symbol="WIN$N")
   run_id = result.publish(name="RSI-Mean-Reversion")
   print(f"Run ID: {run_id}")

Features Populated in the Desktop UI
------------------------------------

1. **Trade Chart**: Interactive candlestick chart displaying executed entry and exit arrows, stop-loss / take-profit levels, and custom indicators declared via :meth:`~q_backend.research.ResearchStrategy.chart_indicators`.
2. **Performance Metrics**: Sharpe ratio, win rate, profit factor, max drawdown, and average trade duration.
3. **Monthly Breakdown**: Heatmap of returns grouped by year and month.
4. **Trade List**: Sortable, filterable list of all trades with entry/exit timestamps, reasons, and individual PnL.
