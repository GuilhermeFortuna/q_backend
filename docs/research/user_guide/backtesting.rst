===========
Backtesting
===========

The Backtest Engine
-------------------

The :func:`~q_backend.research.backtest` function executes strategies against historical price frames using Q's deterministic kernel.

Transaction Costs
-----------------

Costs are modeled via :class:`q_backend.backtesting.costs.TransactionCostConfig`:

- ``cost_per_contract``: Fixed exchange and brokerage fee per contract per side.
- ``cost_bps``: Basis points charged against notional volume.

Example Cost Calibration (Mini-Dollar Futures ``WDO$N``)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

For ``WDO$N`` with ``point_value=10.0``, a 0.5-point tick size, and an assumed brokerage/exchange fee of R$1.25 per side:

.. math::

   \text{Cost Per Side} = \text{Fee} + 0.5 \times \text{Tick Size} \times \text{Point Value} = 1.25 + 0.5 \times 0.5 \times 10.0 = 3.75

.. code-block:: python

   from q_backend.backtesting.costs import TransactionCostConfig
   from q_backend.research import backtest

   costs = TransactionCostConfig(cost_per_contract=3.75)
   result = backtest(bars, strategy=strategy, symbol="WDO$N", point_value=10.0, costs=costs)

Backtest Results
----------------

:class:`~q_backend.research.BacktestResult` contains:

- ``metrics``: Summary statistics (``total_pnl``, ``win_rate``, ``profit_factor``, ``max_drawdown_pct``, etc.).
- ``trades``: DataFrame recording every executed trade.
- ``equity``: DataFrame tracking realized account equity over time.
- ``rejected_entries``: Entries rejected due to price levels invalid at execution time.
- ``data``: Historical frame augmented with computed indicators.
