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
5. **No Service Dependencies**: Bar calculations are pure. Liquidity helpers read an explicit local TickStore; they never fetch or synchronize data and require no gateway, database, or Redis.

Supported indicators
--------------------

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Function
     - Return
     - Description
   * - :func:`~q_backend.research.indicators.ma`
     - ``Series``
     - Moving averages: sma, ema, wma, smma, hma.
   * - :func:`~q_backend.research.indicators.volume_ma`
     - ``Series``
     - Simple moving average of a chosen volume Series.
   * - :func:`~q_backend.research.indicators.vwap`
     - ``Series``
     - Intraday VWAP of typical price, reset at each calendar day; needs high, low, close and volume.
   * - :func:`~q_backend.research.indicators.rsi`
     - ``Series``
     - Wilder RSI on a 0–100 scale.
   * - :func:`~q_backend.research.indicators.macd`
     - ``(line, signal, histogram)``
     - Fast-minus-slow price EMA, signal EMA, and residual.
   * - :func:`~q_backend.research.indicators.bollinger`
     - ``(upper, middle, lower)``
     - SMA plus sample standard-deviation bands.
   * - :func:`~q_backend.research.indicators.donchian`
     - ``(upper, lower)``
     - Highest high and lowest low, including the current bar.
   * - :func:`~q_backend.research.indicators.rolling_zscore`
     - ``Series``
     - Sample z-score of closing prices.
   * - :func:`~q_backend.research.indicators.rolling_rank`
     - ``Series``
     - Fraction of trailing closes less than or equal to the current close.
   * - :func:`~q_backend.research.indicators.pct_change`
     - ``Series``
     - Simple return over a positive bar lookback, as a decimal.
   * - :func:`~q_backend.research.indicators.log_return`
     - ``Series``
     - Log-price return over a positive horizon.
   * - :func:`~q_backend.research.indicators.price_zscore_log`
     - ``Series``
     - Sample z-score of log closing prices.
   * - :func:`~q_backend.research.indicators.ewma_trend_spread`
     - ``Series``
     - Fast-minus-slow EWMA of log prices.
   * - :func:`~q_backend.research.indicators.normalized_momentum`
     - ``Series``
     - Horizon log return scaled by per-bar volatility and sqrt(horizon).
   * - :func:`~q_backend.research.indicators.normalized_ewma_trend_spread`
     - ``Series``
     - Log-price EWMA spread divided by per-bar volatility.
   * - :func:`~q_backend.research.indicators.atr`
     - ``Series``
     - Wilder-smoothed true range in price units.
   * - :func:`~q_backend.research.indicators.atr_fraction`
     - ``Series``
     - ATR divided by the current close, as a fraction.
   * - :func:`~q_backend.research.indicators.true_range`
     - ``Series``
     - Bar range including the gap from the previous close.
   * - :func:`~q_backend.research.indicators.realized_vol`
     - ``Series``
     - Annualized sample deviation of one-bar log returns.
   * - :func:`~q_backend.research.indicators.yang_zhang`
     - ``Series``
     - Annualized OHLC volatility, including opening gaps.
   * - :func:`~q_backend.research.indicators.per_bar_realized_vol`
     - ``Series``
     - Root mean square of trailing log returns, without annualization.
   * - :func:`~q_backend.research.indicators.per_bar_ewma_vol`
     - ``Series``
     - Square root of an EWMA of squared log returns.
   * - :func:`~q_backend.research.indicators.rogers_satchell`
     - ``Series``
     - Per-bar OHLC volatility excluding opening gaps.
   * - :func:`~q_backend.research.indicators.per_bar_yang_zhang`
     - ``Series``
     - Per-bar Yang-Zhang volatility, including opening gaps.
   * - :func:`~q_backend.research.indicators.volatility_log_ratio`
     - ``Series``
     - Log ratio of short-term to long-term per-bar volatility.
   * - :func:`~q_backend.research.indicators.quote_spread`
     - ``Series``
     - Stored bid–ask spread aggregated to bars, in basis points.
   * - :func:`~q_backend.research.indicators.spread_vs_volatility`
     - ``Series``
     - Relative stored quote spread divided by per-bar volatility.
   * - :func:`~q_backend.research.indicators.top_bottom`
     - ``DataFrame``
     - Confirmed tops and bottoms from a causal ATR-threshold zigzag. Each pivot appears on the bar that confirms it, not the bar where it occurred.

Add indicators to a frame
-------------------------

.. code-block:: python

   from q_backend.research import indicators

   bars = bars.assign(
       ema_21=indicators.ma(bars["close"], period=21, kind="ema"),
       atr_14=indicators.atr(bars, period=14),
       rank_20=indicators.rolling_rank(bars["close"], window=20),
       momentum=indicators.normalized_momentum(bars, horizon=20),
       volume_ma=indicators.volume_ma(bars["tick_volume"], period=20),
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
The required warm-up depends on the indicator and its parameters. ``true_range``
leaves its first bar NaN because no previous close is available.

Choose volatility units
-----------------------

``realized_vol`` uses close-to-close log returns. ``yang_zhang`` also accounts for
open-to-close moves and overnight gaps. Both accept ``periods_per_year`` to
annualize volatility; the default is 252. For intraday bars, choose a value that
matches the number of observations in your assumed trading year rather than
using the daily default unchanged.


Return and rank units
---------------------

``pct_change`` returns decimals: 0.01 means a 1% price increase. ``log_return``
uses natural logarithms and is not multiplied by 100. ``rolling_rank`` returns
count(window values <= current close) / window, including ties: a constant
window ranks 1, and its minimum rank is 1/window. Z-scores use sample standard
deviation; a constant window has an undefined score and leaves NaN values.

Per-bar volatility and composites
---------------------------------

``per_bar_realized_vol`` is the root mean square of log returns, without
subtracting their mean. It differs from the centered sample deviation used by
``realized_vol``. ``per_bar_ewma_vol`` smooths squared log returns with a half-life
in bars. ``rogers_satchell`` and ``per_bar_yang_zhang`` use OHLC information; only
the latter includes opening gaps. These helpers are not annualized.

Composite helpers accept ``vol_estimator="realized"`` (the default), ``"ewma"``,
``"rogers_satchell"``, or ``"yang_zhang"``. A trailing ``vol_window`` is required
for the window estimators and must be at least 2 for Yang-Zhang. EWMA uses a
positive ``vol_half_life`` instead. The unused window or half-life is ignored,
and can be None. ``vol_lag=1`` uses the previous bar's volatility; 0 uses the
current bar's value. Missing or near-zero volatility leaves NaN ratios.

.. code-block:: python

   bars["momentum"] = indicators.normalized_momentum(
       bars, horizon=20, vol_estimator="ewma", vol_half_life=16.0,
   )
   bars["trend"] = indicators.normalized_ewma_trend_spread(
       bars, fast_half_life=8.0, slow_half_life=32.0,
   )
   bars["vol_regime"] = indicators.volatility_log_ratio(
       bars, short_window=10, long_window=40,
   )

Fast half-lives must be smaller than slow half-lives. For a volatility ratio,
the short window must be smaller than the long window; when using EWMA this
ordering applies to the half-lives instead. Integer parameters reject booleans
and fractional values; numeric parameters must be finite. Price/volume inputs
must be real numeric data; complex dtypes are rejected before float conversion. Research helpers
retain the existing unbounded lookback convention, without the HTTP catalog's
resource caps.

Liquidity from stored quotes
----------------------------

Pass an existing :class:`~q_backend.research.TickStore` and an explicit fixed
``timeframe``. The supported intervals are M1, M5, M10, M15, H1, H2, H4, and D1,
matching the shared quote service. The frame must have unique, chronological,
non-overlapping bar-open timestamps in a DatetimeIndex. Aware times are converted
to Brasilia for quote alignment; naive times are interpreted as Brasilia local
time. Output keeps the original index and timezone. If ``q_research`` metadata
is present, its symbol and timeframe must agree with the store and arguments.

.. code-block:: python

   from q_backend.research import TickStore, indicators

   ticks = TickStore("WIN$N")  # Existing, locally synchronized quote history.
   bars["spread_bps"] = indicators.quote_spread(
       bars, ticks=ticks, timeframe="M5", min_coverage=0.5,
   )
   bars["spread_ratio"] = indicators.spread_vs_volatility(
       bars, ticks=ticks, timeframe="M5", vol_window=20,
   )

``quote_spread`` returns basis points: 1 bp = 0.01%. ``spread_vs_volatility``
divides relative spread by per-bar volatility and returns a dimensionless ratio;
it does not divide a basis-point value by volatility.

Both default to time-weighted mean spread, a 60,000ms maximum quote age, and
zero minimum coverage. Choose ``output="last"`` or ``"max"`` for quoted spread;
use ``spread_output`` for the ratio. ``last`` means a quote valid at the bar's
close. Minimum coverage is a fraction between 0 and 1. Stale or insufficiently
covered bars are NaN, and quotes never carry across sessions.

These helpers only read stored files. They never synchronize missing history,
contact a gateway, or load additional warm-up bars. If no requested session has
ticks, all values are NaN and ``result.attrs["unavailable_reason"]`` contains the
shared quote service's explanation. That attribute is None for an empty result
or when some requested session has ticks, even if its quotes are unusable.
