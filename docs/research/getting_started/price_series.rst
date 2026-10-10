=======================
Choosing a price series
=======================

A continuous future joins successive contracts into one history. The way it
adjusts prices at a contract roll matters because Q computes PnL in price points.
For the B3 series used in these examples, the suffix identifies the adjustment:


.. list-table::
   :header-rows: 1
   :widths: 15 25 30 30

   * - Suffix
     - Adjustment
     - How prices change
     - What it preserves
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

Impact on point-based backtesting
---------------------------------

:func:`~q_backend.research.backtest` evaluates profit and loss in point differences:

.. math::

   \text{PnL} = \Delta \text{Price} \times \text{Quantity} \times \text{Point Value} - \text{Costs}

Proportional adjustment rescales historical price moves while fixed per-contract
costs stay unchanged. A point-based backtest on that series can therefore change
with the adjustment rather than the strategy. Rescaled prices can also fall off
the exchange tick grid.

Recommendations
---------------

- **Intraday strategies (flat at EOD):** Use the unadjusted series (``$N``).
- **Multi-day swing strategies:** Use difference-adjusted series (``$D``) to preserve point-based PnL across rolls.
- **Percentage-return analysis only:** Use proportional adjustment (``$``) only if ignoring contract multipliers and fixed costs.

Tick grid verification
----------------------

:func:`~q_backend.research.load_bars` performs an automated tick-grid validation check. If more than 1% of prices are not integer multiples of the instrument's ``trade_tick_size``, it emits :class:`~q_backend.research.AdjustedSeriesWarning`.
