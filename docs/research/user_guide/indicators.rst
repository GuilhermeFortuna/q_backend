====================
Technical Indicators
====================

The :mod:`q_backend.research.indicators` module provides functional helpers backed by Q's high-performance Rust calculations (``q_core``).

Design Principles
-----------------

1. **Exact Index Preservation**: All returned Series share the exact index, timezone, and name of the input.
2. **Immutability**: Input Series and DataFrames are never mutated.
3. **Strict Causality**: Calculations depend only on rows at or prior to the current bar.
4. **NaN Warm-Up**: Initial lookback bars contain ``NaN``. No forward-filling or zero-filling is applied.
5. **No Service Dependencies**: Pure functional operations requiring no gateway, database, or Redis.

Supported Indicators
--------------------

.. list-table::
   :header-rows: 1
   :widths: 25 20 55

   * - Function
     - Return Type
     - Description
   * - :func:`~q_backend.research.indicators.ma`
     - :class:`pandas.Series`
     - Moving average: ``sma``, ``ema``, ``wma``, ``smma``, ``hma``.
   * - :func:`~q_backend.research.indicators.rsi`
     - :class:`pandas.Series`
     - Wilder's Relative Strength Index (0 to 100).
   * - :func:`~q_backend.research.indicators.atr`
     - :class:`pandas.Series`
     - Wilder's Average True Range.
   * - :func:`~q_backend.research.indicators.bollinger`
     - ``tuple[Series, Series, Series]``
     - Upper, middle, and lower Bollinger Bands.
   * - :func:`~q_backend.research.indicators.macd`
     - ``tuple[Series, Series, Series]``
     - MACD line, signal line, and histogram.
   * - :func:`~q_backend.research.indicators.donchian`
     - ``tuple[Series, Series]``
     - Upper and lower Donchian Channels.
   * - :func:`~q_backend.research.indicators.realized_vol`
     - :class:`pandas.Series`
     - Annualized close-to-close realized volatility from log returns.
   * - :func:`~q_backend.research.indicators.yang_zhang`
     - :class:`pandas.Series`
     - Minimum variance Yang-Zhang (2000) historical volatility.

Mathematical Formulation of Yang-Zhang (2000)
---------------------------------------------

Yang-Zhang historical volatility handles both continuous price drift and overnight jump openings:

.. math::

   \sigma^2 = \sigma_o^2 + k \cdot \sigma_c^2 + (1 - k) \cdot \sigma_{rs}^2

where :math:`k = \frac{0.34}{1.34 + \frac{n + 1}{n - 1}}`, :math:`\sigma_o` is overnight jump volatility, :math:`\sigma_c` is open-to-close volatility, and :math:`\sigma_{rs}` is the Rogers-Satchell variance estimator:

.. math::

   \sigma_{rs}^2 = \frac{1}{n} \sum \left( u(u - c) + d(d - c) \right)
