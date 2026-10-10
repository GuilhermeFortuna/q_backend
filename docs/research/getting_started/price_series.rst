=======================
Choosing a Price Series
=======================

When conducting research on B3 futures (such as Mini-Ibovespa ``WIN`` or Mini-Dólar ``WDO``), MetaTrader 5 provides each continuous contract in three forms:

.. list-table::
   :header-rows: 1
   :widths: 15 25 30 30

   * - Suffix
     - Broker Description
     - Price Formulation
     - Preservation Property
   * - ``$N`` (e.g. ``WIN$N``)
     - Sem Ajustes
     - As traded; price gap at each contract roll
     - Exact point differences and tick grid alignment
   * - ``$D`` (e.g. ``WIN$D``)
     - Ajuste por Diferença
     - Shifted by an additive constant at each roll
     - Inter-bar point differences across rolls
   * - ``$`` (e.g. ``WIN$``)
     - Ajuste Proporcional
     - Multiplied by roll factor at each roll
     - Cumulative percentage returns only

Impact on Point-Based Backtesting
---------------------------------

:func:`~q_backend.research.backtest` evaluates profit and loss in point differences:

.. math::

   \text{PnL} = \Delta \text{Price} \times \text{Quantity} \times \text{Point Value} - \text{Costs}

On proportionally adjusted series (``$``), historical price moves are scaled down while per-contract fixed costs remain constant, severely distorting backtests. Furthermore, proportional adjustment causes price levels to fall off exchange tick grids.

Recommendations
---------------

- **Intraday strategies (flat at EOD):** Use the unadjusted series (``$N``).
- **Multi-day swing strategies:** Use difference-adjusted series (``$D``) to preserve point-based PnL across rolls.
- **Percentage-return analysis only:** Use proportional adjustment (``$``) only if ignoring contract multipliers and fixed costs.

Tick Grid Verification
----------------------

:func:`~q_backend.research.load_bars` performs an automated tick-grid validation check. If more than 1% of prices are not integer multiples of the instrument's ``trade_tick_size``, it emits :class:`~q_backend.research.AdjustedSeriesWarning`.
