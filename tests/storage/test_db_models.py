from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db import execution_models  # noqa: F401

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
    "strategy_search_runs",
    "strategy_search_candidates",
    "feature_definitions",
    "feature_versions",
    "evaluation_runs",
    "feature_score_rows",
    "feature_evidence_rows",
    "neural_models",
    "neural_model_versions",
    "paper_accounts",
    "execution_control_state",
    "execution_deployments",
    "execution_decisions",
    "execution_orders",
    "execution_fills",
    "execution_net_positions",
    "execution_ledger_entries",
    "execution_risk_events",
    "execution_worker_leases",
    "execution_audit_events",
}

FORBIDDEN_TABLE_NAMES = {"ohlcv", "ticks", "bars", "features", "tick", "candles"}


def test_metadata_has_twenty_nine_tables():
    table_names = set(Base.metadata.tables.keys())
    assert table_names == EXPECTED_TABLES


def test_no_market_data_tables():
    table_names = set(Base.metadata.tables.keys())
    assert table_names.isdisjoint(FORBIDDEN_TABLE_NAMES)
