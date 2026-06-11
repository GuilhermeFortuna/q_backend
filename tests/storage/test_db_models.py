from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401

EXPECTED_TABLES = {
    "strategies",
    "strategy_versions",
    "backtest_configs",
    "backtest_runs",
    "optimization_studies",
    "optimization_trials",
    "data_ingestion_runs",
    "walkforward_runs",
    "walkforward_windows",
}

FORBIDDEN_TABLE_NAMES = {"ohlcv", "ticks", "bars", "features", "tick", "candles"}


def test_metadata_has_nine_tables():
    table_names = set(Base.metadata.tables.keys())
    assert table_names == EXPECTED_TABLES


def test_no_market_data_tables():
    table_names = set(Base.metadata.tables.keys())
    assert table_names.isdisjoint(FORBIDDEN_TABLE_NAMES)
