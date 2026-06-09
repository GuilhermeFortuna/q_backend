import uuid
from datetime import datetime, timezone

from q_backend.storage.db.models import DatasetType, RunStatus, TrialStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    create_data_ingestion_run,
    create_optimization_study,
    create_optimization_trial,
    create_strategy,
    create_strategy_version,
    delete_backtest_run,
    delete_backtest_runs,
    delete_optimization_study,
    delete_optimization_studies,
    find_backtest_run_by_config,
    get_backtest_run,
    get_optimization_study,
    get_or_create_strategy,
    list_backtest_runs,
    list_optimization_studies,
    update_backtest_run,
    update_data_ingestion_run,
    update_optimization_study,
    update_optimization_trial,
)


def test_strategy_and_version_flow(db_session):
    strategy = create_strategy(db_session, name="MACrossover", description="MA crossover")
    version = create_strategy_version(
        db_session,
        strategy_id=strategy.id,
        version=1,
        config={"fast": 9, "slow": 21},
        status=RunStatus.COMPLETED.value,
    )
    assert version.strategy_id == strategy.id
    assert version.version == 1


def test_backtest_config_and_run_flow(db_session):
    config = create_backtest_config(
        db_session,
        name="petr4-d1",
        config={"symbol": "PETR4", "timeframe": "D1"},
    )
    run = create_backtest_run(
        db_session,
        backtest_config_id=config.id,
        config={"initial_capital": 100_000},
        status=RunStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc),
    )
    updated = update_backtest_run(
        db_session,
        run.id,
        status=RunStatus.COMPLETED.value,
        result_summary={"net_profit": 1234.5},
        lake_paths={"equity": "data/lake/backtest/equity.parquet"},
        finished_at=datetime.now(timezone.utc),
    )
    assert updated.status == RunStatus.COMPLETED.value
    assert updated.result_summary["net_profit"] == 1234.5


def test_optimization_study_and_trial_flow(db_session):
    study = create_optimization_study(
        db_session,
        name="ma_sharpe",
        config={"n_trials": 50},
        status=RunStatus.RUNNING.value,
    )
    trial = create_optimization_trial(
        db_session,
        study_id=study.id,
        trial_number=0,
        params={"fast": 9, "slow": 21},
        status=TrialStatus.RUNNING.value,
    )
    updated_study = update_optimization_study(
        db_session,
        study.id,
        status=RunStatus.COMPLETED.value,
    )
    updated_trial = update_optimization_trial(
        db_session,
        trial.id,
        status=TrialStatus.COMPLETED.value,
        metrics={"sharpe": 1.2},
    )
    assert updated_study.status == RunStatus.COMPLETED.value
    assert updated_trial.metrics["sharpe"] == 1.2


def test_data_ingestion_run_flow(db_session):
    run = create_data_ingestion_run(
        db_session,
        source="mt5",
        symbol="PETR4",
        dataset_type=DatasetType.OHLCV.value,
        timeframe="D1",
        status=RunStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc),
    )
    updated = update_data_ingestion_run(
        db_session,
        run.id,
        status=RunStatus.COMPLETED.value,
        lake_path="data/lake/ohlcv/PETR4/D1/part-0001.parquet",
        stats={"rows": 1000},
        finished_at=datetime.now(timezone.utc),
    )
    assert updated.lake_path.endswith(".parquet")
    assert updated.stats["rows"] == 1000


def test_get_or_create_strategy_returns_existing(db_session):
    first = create_strategy(db_session, name="MACrossover")
    second = get_or_create_strategy(db_session, name="MACrossover")
    assert second.id == first.id


def test_get_or_create_strategy_creates_when_missing(db_session):
    strategy = get_or_create_strategy(db_session, name="NewStrategy")
    assert strategy.name == "NewStrategy"


def test_get_backtest_run(db_session):
    config = create_backtest_config(
        db_session,
        name="win-m5",
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
    )
    run = create_backtest_run(
        db_session,
        backtest_config_id=config.id,
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
        status=RunStatus.COMPLETED.value,
    )
    fetched = get_backtest_run(db_session, run.id)
    assert fetched is not None
    assert fetched.id == run.id


def test_delete_backtest_run(db_session):
    config = create_backtest_config(
        db_session,
        name="win-m5",
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
    )
    run = create_backtest_run(
        db_session,
        backtest_config_id=config.id,
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
        status=RunStatus.COMPLETED.value,
    )

    assert delete_backtest_run(db_session, run.id) is True
    assert get_backtest_run(db_session, run.id) is None
    assert delete_backtest_run(db_session, run.id) is False


def test_list_backtest_runs_newest_first_and_symbol_filter(db_session):
    config_a = create_backtest_config(
        db_session,
        name="petr4-d1",
        config={"symbol": "PETR4", "timeframe": "D1", "strategy": "MACrossover"},
    )
    config_b = create_backtest_config(
        db_session,
        name="win-m5",
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
    )
    older = create_backtest_run(
        db_session,
        backtest_config_id=config_a.id,
        config={"symbol": "PETR4", "timeframe": "D1", "strategy": "MACrossover"},
        status=RunStatus.COMPLETED.value,
    )
    newer = create_backtest_run(
        db_session,
        backtest_config_id=config_b.id,
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
        status=RunStatus.COMPLETED.value,
    )
    older.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db_session.flush()

    all_runs, total = list_backtest_runs(db_session)
    assert total == 2
    assert all_runs[0].id == newer.id
    assert all_runs[1].id == older.id

    win_runs, win_total = list_backtest_runs(db_session, symbol="WIN$")
    assert win_total == 1
    assert win_runs[0].id == newer.id


def test_find_backtest_run_by_config_returns_newest_match(db_session):
    config = {
        "symbol": "WIN$",
        "timeframe": "M5",
        "strategy": "MACrossover",
        "strategy_params": {"short_period": 5, "long_period": 10},
    }
    config_row = create_backtest_config(db_session, name="win-m5", config=config)
    older = create_backtest_run(
        db_session,
        backtest_config_id=config_row.id,
        config=config,
        status=RunStatus.COMPLETED.value,
    )
    newer_config = create_backtest_config(db_session, name="win-m5-copy", config=config)
    newer = create_backtest_run(
        db_session,
        backtest_config_id=newer_config.id,
        config=config,
        status=RunStatus.COMPLETED.value,
    )
    older.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db_session.flush()

    match = find_backtest_run_by_config(db_session, config)
    assert match is not None
    assert match.id == newer.id


def test_find_backtest_run_by_config_returns_none_when_no_match(db_session):
    config = create_backtest_config(
        db_session,
        name="win-m5",
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
    )
    create_backtest_run(
        db_session,
        backtest_config_id=config.id,
        config={"symbol": "WIN$", "timeframe": "M5", "strategy": "MACrossover"},
        status=RunStatus.COMPLETED.value,
    )

    assert find_backtest_run_by_config(
        db_session,
        {"symbol": "WIN$", "timeframe": "M5", "strategy": "RSI"},
    ) is None


def _create_completed_run(db_session, *, symbol, strategy, pnl=None, is_saved=False):
    config = create_backtest_config(
        db_session,
        name=f"{symbol}-{strategy}",
        config={"symbol": symbol, "timeframe": "M5", "strategy": strategy},
    )
    run = create_backtest_run(
        db_session,
        backtest_config_id=config.id,
        config={"symbol": symbol, "timeframe": "M5", "strategy": strategy},
        status=RunStatus.COMPLETED.value,
    )
    summary = {"total_pnl": pnl} if pnl is not None else None
    update_backtest_run(
        db_session,
        run.id,
        result_summary=summary,
        is_saved=is_saved,
    )
    return run


def test_list_backtest_runs_strategy_and_saved_filters(db_session):
    _create_completed_run(db_session, symbol="WIN$", strategy="MACrossover", is_saved=True)
    _create_completed_run(db_session, symbol="WIN$", strategy="RSI", is_saved=False)
    _create_completed_run(db_session, symbol="WDO$", strategy="MACrossover", is_saved=True)

    rsi_runs, rsi_total = list_backtest_runs(db_session, strategy="RSI")
    assert rsi_total == 1
    assert rsi_runs[0].config["strategy"] == "RSI"

    saved_runs, saved_total = list_backtest_runs(db_session, saved_only=True)
    assert saved_total == 2
    assert all(run.is_saved for run in saved_runs)


def test_list_backtest_runs_pnl_sort(db_session):
    low = _create_completed_run(db_session, symbol="WIN$", strategy="A", pnl=100.0)
    high = _create_completed_run(db_session, symbol="WIN$", strategy="B", pnl=5000.0)
    none_pnl = _create_completed_run(db_session, symbol="WIN$", strategy="C", pnl=None)

    desc_runs, _ = list_backtest_runs(db_session, sort="pnl_desc")
    assert desc_runs[0].id == high.id
    assert desc_runs[1].id == low.id
    assert desc_runs[-1].id == none_pnl.id

    asc_runs, _ = list_backtest_runs(db_session, sort="pnl_asc")
    assert asc_runs[0].id == low.id
    assert asc_runs[1].id == high.id
    assert asc_runs[-1].id == none_pnl.id


def test_delete_backtest_runs_bulk(db_session):
    run_a = _create_completed_run(db_session, symbol="WIN$", strategy="A")
    run_b = _create_completed_run(db_session, symbol="WIN$", strategy="B")
    missing_id = uuid.uuid4()

    deleted, not_found = delete_backtest_runs(db_session, [run_a.id, run_b.id, missing_id])
    assert deleted == 2
    assert not_found == [missing_id]
    assert get_backtest_run(db_session, run_a.id) is None


def test_delete_optimization_studies_bulk(db_session):
    study_a = create_optimization_study(
        db_session,
        name="a",
        config={"study": {"n_trials": 1}},
        status=RunStatus.COMPLETED.value,
    )
    study_b = create_optimization_study(
        db_session,
        name="b",
        config={"study": {"n_trials": 1}},
        status=RunStatus.COMPLETED.value,
    )
    missing_id = uuid.uuid4()

    deleted, not_found = delete_optimization_studies(
        db_session, [study_a.id, study_b.id, missing_id]
    )
    assert deleted == 2
    assert not_found == [missing_id]
    study = create_optimization_study(
        db_session,
        name="ma_sharpe",
        config={"study": {"n_trials": 10}},
        status=RunStatus.RUNNING.value,
    )
    create_optimization_trial(
        db_session,
        study_id=study.id,
        trial_number=0,
        params={"fast": 9},
        status=TrialStatus.COMPLETED.value,
    )
    fetched = get_optimization_study(db_session, study.id)
    assert fetched is not None
    assert fetched.id == study.id
    assert len(fetched.trials) == 1


def test_delete_optimization_study(db_session):
    study = create_optimization_study(
        db_session,
        name="ma_sharpe",
        config={"study": {"n_trials": 10}},
        status=RunStatus.COMPLETED.value,
    )
    create_optimization_trial(
        db_session,
        study_id=study.id,
        trial_number=0,
        params={"fast": 9},
        status=TrialStatus.COMPLETED.value,
    )

    assert delete_optimization_study(db_session, study.id) is True
    assert get_optimization_study(db_session, study.id) is None
    assert delete_optimization_study(db_session, study.id) is False


def test_list_optimization_studies_newest_first(db_session):
    older = create_optimization_study(
        db_session,
        name="older",
        config={"study": {"n_trials": 5}},
        status=RunStatus.COMPLETED.value,
    )
    newer = create_optimization_study(
        db_session,
        name="newer",
        config={"study": {"n_trials": 10}},
        status=RunStatus.COMPLETED.value,
    )
    older.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db_session.flush()

    studies, total = list_optimization_studies(db_session)
    assert total == 2
    assert studies[0].id == newer.id
    assert studies[1].id == older.id
