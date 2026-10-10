====================
Technical indicators
====================

The :mod:`q_backend.research.indicators` module provides functional helpers backed by Q's high-performance Rust calculations (``q_core``).

Design principles
-----------------

1. **Exact Index Preservation**: Returned Series align with the input index, including its timezone.
2. **Immutability**: Input Series and DataFrames are never mutated.
3. **Strict Causality**: Calculations depend only on rows at or prior to the current bar.
4. **NaN Warm-Up**: Initial lookback bars contain ``NaN``. No forward-filling or zero-filling is applied.
5. **No Service Dependencies**: Pure functional operations requiring no gateway, database, or Redis.

Supported indicators
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

Add indicators to a frame
-------------------------

.. code-block:: python

   from q_backend.research import indicators

   bars = bars.assign(
       ema_21=indicators.ma(bars["close"], period=21, kind="ema"),
       atr_14=indicators.atr(bars, period=14),
   )
   upper, middle, lower = indicators.bollinger(bars["close"], period=20)
   bars = bars.assign(bb_upper=upper, bb_middle=middle, bb_lower=lower)

Price-only functions take a Series; ATR, Donchian, and Yang-Zhang take a
DataFrame with the required price columns. See :doc:`../reference/indicators`
for each function's inputs and output order.

Handle warm-up values
---------------------

Early rows can contain ``NaN`` until enough observations are available. Leave
those values in place and guard decisions that need a valid indicator. Filling
with future values introduces lookahead; filling with zero changes the signal.
The required warm-up depends on the indicator and its parameters.

Choose volatility units
-----------------------

``realized_vol`` uses close-to-close log returns. ``yang_zhang`` also accounts for
open-to-close moves and overnight gaps. Both accept ``periods_per_year`` to
annualize volatility; the default is 252. For intraday bars, choose a value that
matches the number of observations in your assumed trading year rather than
using the daily default unchanged.
