===========
Backtesting
===========

:func:`~q_backend.research.backtest` runs synchronously in your Python process
and returns a :class:`~q_backend.research.BacktestResult`. It accepts a custom
:class:`~q_backend.research.ResearchStrategy` instance or a registered strategy
name. Start with :doc:`../getting_started/quickstart` for a complete example.

Choose the units
----------------

``quantity`` is the number of contracts. ``point_value`` is the account-currency
value of one price point per contract. Profit and loss is based on price
**differences**, with direction determining the sign:

.. math::

   \text{PnL} = \text{signed price change} \times \text{quantity}
   \times \text{point value} - \text{costs}

For continuous futures, read :doc:`../getting_started/price_series` before
choosing your data. :doc:`execution` explains when entries and exits fill.

Include transaction costs
-------------------------

``costs=None`` models zero costs. Pass
:class:`q_backend.backtesting.costs.TransactionCostConfig` to include:

* ``cost_per_contract``: a fixed amount per contract **on each side** of a trade.
* ``cost_bps``: basis points charged on traded notional value.

For an illustrative mini-dollar calculation, assume a R$1.25 fee per side,
a 0.5-point spread, and ``point_value=10.0``. Adding half the spread gives:

.. math::

   \text{cost per side} = 1.25 + 0.5 \times 0.5 \times 10.0 = 3.75

.. code-block:: python

   from q_backend.backtesting.costs import TransactionCostConfig
   from q_backend.research import backtest

   costs = TransactionCostConfig(cost_per_contract=3.75)
   result = backtest(
       bars, strategy=strategy, symbol="WDO$N", point_value=10.0, costs=costs,
   )

That is R$7.50 per round trip for one contract. These are example assumptions,
not a broker fee schedule. The configured cost applies to each fill, including
protective and custom tick exits.

Read the result
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Attribute
     - Use it to inspect
   * - ``metrics``
     - Summary statistics such as ``total_pnl``, ``win_rate``, and ``profit_factor``
   * - ``trades``
     - Entry/exit times, prices, quantity, commissions, PnL, and exit reasons
   * - ``equity``
     - Realized equity over time; open positions are not marked to market
   * - ``data``
     - Input prices enriched with strategy and exit indicators
   * - ``rejected_entries``
     - Entries whose protective levels were invalid at the fill price
   * - ``config``
     - The recorded run arguments, exposed as a read-only mapping
   * - ``indicators``
     - Chart series declared by the strategy

Check ``trades`` and ``rejected_entries`` alongside the metrics. To save the
finished result for visual review, continue with :doc:`publishing`.
