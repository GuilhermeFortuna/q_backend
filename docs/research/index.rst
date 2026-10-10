.. Q Research documentation master file

=================================
Q Research Library Documentation
=================================

**Q Research** is the quantitative research and local backtesting library of the Q platform.
It provides high-performance, pandas-native tools for market data ingestion, tick resampling,
Rust-backed indicators, custom strategy authoring, intrabar tick simulation, and local backtesting.

.. grid:: 1 2 2 3
    :gutter: 3

    .. grid-item-card:: 🚀 Getting Started
        :link: getting_started/index
        :link-type: doc

        Connect to MT5, fetch continuous futures bars, and run your first strategy in 10 minutes.

    .. grid-item-card:: 📖 User Guide
        :link: user_guide/index
        :link-type: doc

        In-depth guides on tick caching, intrabar execution, indicators, and transaction costs.

    .. grid-item-card:: 🔍 API Reference
        :link: reference/index
        :link-type: doc

        Exhaustive reference for ``load_bars``, ``indicators``, ``backtest``, and ``TickStore``.

.. toctree::
   :maxdepth: 2
   :hidden:

   getting_started/index
   user_guide/index
   reference/index
