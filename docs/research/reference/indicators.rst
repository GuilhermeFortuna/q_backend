====================
Technical Indicators
====================

Compute indicators from pandas Series and DataFrames. Every Market chart catalog
indicator is available here, with moving averages selected through ``ma(kind=...)``.
Outputs align with the input index; see :doc:`../user_guide/indicators` for examples,
units, warm-up handling, and stored quote requirements.

.. currentmodule:: q_backend.research.indicators

Moving averages and oscillators
-------------------------------

.. autosummary::
   :toctree: api/

   ma
   volume_ma
   rsi
   macd

Transforms and trend
--------------------

.. autosummary::
   :toctree: api/

   rolling_zscore
   rolling_rank
   pct_change
   log_return
   price_zscore_log
   ewma_trend_spread
   normalized_momentum
   normalized_ewma_trend_spread

Channels and volatility
-----------------------

.. autosummary::
   :toctree: api/

   atr
   atr_fraction
   true_range
   bollinger
   donchian
   realized_vol
   yang_zhang
   per_bar_realized_vol
   per_bar_ewma_vol
   rogers_satchell
   per_bar_yang_zhang
   volatility_log_ratio
   top_bottom

Liquidity from stored quotes
----------------------------

.. autosummary::
   :toctree: api/

   quote_spread
   spread_vs_volatility
