"""Tests for deploying research strategy versions (Q-136 acceptance 3 & 7)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.schemas.execution import (
    DeploymentConfigurationUpdateRequest,
    DeploymentCreateRequest,
    PaperAccountCreateRequest,
    PaperCostConfigInput,
)
from q_backend.api.services import execution as execution_service
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.brokers.routing import BrokerRouter
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
)
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.quote_source import EdgeQuoteSource
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.execution.service import ExecutionService
from q_backend.execution.validation import ExecutionValidationError, resolve_saved_backtest
from q_backend.execution.strategy_build import build_research_strategy_from_compiled
from q_backend.research.strategy_versions import freeze_strategy_file
from q_backend.storage.db.base import Base
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db.models import BacktestConfig, BacktestRun, ResearchStrategyVersion
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    update_backtest_run,
)
from tests.execution.fake_edge import fake_edge_server, make_filled_lookup_deal

MARKET_ONLY_STRATEGY = """from __future__ import annotations

from q_backend.research import IntParam, ResearchStrategy, TradeOrder


class MarketTrend(ResearchStrategy):
    parameters = {"period": IntParam(min=1, max=100, label="Period")}

    def __init__(self, period: int = 10):
        self.period = period

    def compute_indicators(self, frame):
        out = frame.copy()
        out["sma"] = out["close"].rolling(self.period, min_periods=1).mean()
        return out

    def entry_strategy(self, frame, positions=()):
        if not positions:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        return None
"""


@pytest.fixture
def api_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_db_session(api_db_engine) -> Session:
    session_factory = sessionmaker(bind=api_db_engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def frozen_version(api_db_session: Session, tmp_path: Path) -> ResearchStrategyVersion:
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    (strat_dir / "market_trend.py").write_text(MARKET_ONLY_STRATEGY, encoding="utf-8")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    version = freeze_strategy_file(
        api_db_session,
        strat_dir,
        "market_trend",
        bundle_root=bundle_dir,
    )
    api_db_session.flush()
    return version


def _create_saved_run(
    session: Session,
    version: ResearchStrategyVersion,
    *,
    order_kinds: list[str] | None = None,
    timeframe: str = "M5",
    day_trade: bool = True,
    symbol: str = "WIN$",
) -> BacktestRun:
    cfg = {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": version.strategy_id,
        "strategy_params": {"period": 10},
        "day_trade": day_trade,
        "day_trade_start_time": "09:00",
        "day_trade_end_time": "16:00",
        "day_trade_close_time": "17:00",
    }
    config = create_backtest_config(
        session,
        name="test-run-cfg",
        config=cfg,
        strategy_version_id=version.id,
    )
    summary = {}
    if order_kinds is not None:
        summary["order_kinds"] = order_kinds

    run = create_backtest_run(
        session,
        backtest_config_id=config.id,
        config=cfg,
        origin="strategy_file",
        strategy_version_id=version.id,
    )
    run.is_saved = True
    run.result_summary = summary
    session.flush()
    return run


def test_create_deployment_from_market_only_strategy_run_succeeds(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-m5", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M5",
        day_trade=True,
    )

    detail = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="live-market-trend",
            source_backtest_run_id=run.id,
        ),
    )

    assert detail.name == "live-market-trend"
    assert detail.source_kind == "strategy_version"
    assert detail.source_strategy_name == "market_trend"
    assert detail.strategy_version_id == frozen_version.id
    assert detail.strategy_bundle_hash == frozen_version.bundle_hash
    assert detail.timeframe == "M5"
    assert detail.symbol == "WIN$"
    assert detail.day_trade_session is not None
    assert detail.day_trade_session.day_trade is True
    assert detail.day_trade_session.start_time == "09:00"
    assert detail.day_trade_session.close_time == "17:00"


M1_DECISION_STRATEGY = MARKET_ONLY_STRATEGY.replace(
    "class MarketTrend(ResearchStrategy):\n",
    'class MarketTrend(ResearchStrategy):\n    decision_timeframe = "M1"\n\n',
)


@pytest.fixture
def frozen_m1_version(api_db_session: Session, tmp_path: Path) -> ResearchStrategyVersion:
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    (strat_dir / "market_trend.py").write_text(M1_DECISION_STRATEGY, encoding="utf-8")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    version = freeze_strategy_file(api_db_session, strat_dir, "market_trend", bundle_root=bundle_dir)
    api_db_session.flush()
    return version


def test_a_version_declaring_m1_deploys_at_m1(
    api_db_session: Session,
    frozen_m1_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-m1", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_m1_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M1",
        day_trade=True,
    )

    detail = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="live-m1-trend",
            source_backtest_run_id=run.id,
        ),
    )

    assert detail.timeframe == "M1"
    assert detail.source_kind == "strategy_version"


def test_a_version_declaring_m1_is_refused_at_any_other_timeframe(
    api_db_session: Session,
    frozen_m1_version: ResearchStrategyVersion,
):
    run = _create_saved_run(
        api_db_session,
        frozen_m1_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M1",
        day_trade=True,
    )

    with pytest.raises(ExecutionValidationError, match="can only be deployed at M1"):
        resolve_saved_backtest(api_db_session, source_backtest_run_id=run.id, timeframe="M5")


def test_create_deployment_refuses_run_without_order_kinds(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-no-kinds", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=None,  # No order_kinds in result_summary
        timeframe="M5",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="no-kinds",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "order_kinds" in str(exc.value.detail)


def test_create_deployment_refuses_non_deployable_order_kinds(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-non-deployable", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "priced_close"],
        timeframe="M5",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="priced-close",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "non-deployable" in str(exc.value.detail)


def test_create_deployment_refuses_sub_m5_timeframe(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-sub-m5", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M1",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="too-fast-m1",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "M5" in str(exc.value.detail)


def test_create_deployment_refuses_run_without_day_trading(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-swing", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M5",
        day_trade=False,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="swing-strategy",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "day-trading" in str(exc.value.detail)


def test_patch_strategy_version_configuration(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-patch", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M5",
        day_trade=True,
    )
    deployment = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="patch-target",
            source_backtest_run_id=run.id,
        ),
    )

    # Invalid parameter: out of range (max is 100)
    with pytest.raises(HTTPException) as exc:
        execution_service.patch_deployment_configuration(
            api_db_session,
            deployment.id,
            DeploymentConfigurationUpdateRequest(
                expected_revision=1,
                actor="test-operator",
                strategy_params={"period": 200},
                sizing_config={"type": "fixed_quantity", "quantity": 1},
            ),
        )
    assert exc.value.status_code == 422

    # Valid parameter: period=20
    revised = execution_service.patch_deployment_configuration(
        api_db_session,
        deployment.id,
        DeploymentConfigurationUpdateRequest(
            expected_revision=1,
            actor="test-operator",
            strategy_params={"period": 20},
            sizing_config={"type": "fixed_quantity", "quantity": 2},
        ),
    )
    assert revised.config_revision == 2
    assert revised.compiled_config["strategy_params"] == {"period": 20}
    assert revised.compiled_config["symbol"] == "WIN$"
    assert revised.compiled_config["timeframe"] == "M5"
    assert revised.strategy_version_id == frozen_version.id
    assert revised.day_trade_session is not None
    assert revised.day_trade_session.day_trade is True


def test_acceptance_7_deployment_submits_entry_and_day_trade_close_against_fake_edge(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
    monkeypatch,
):
    """Acceptance 7: Against the fake edge and a Q-135 demo account, deployment submits entry and day-trade close."""
    from q_backend.backtesting.models import OrderAction, Trade
    from q_backend.execution.domain import DecisionOutcome, ExecutionSide, StrategyIdentity
    from q_backend.storage.db.execution_repositories import acquire_worker_lease, get_open_net_position
    from tests.execution.test_worker_on_edge import _components
    import pandas as pd
    from q_backend.market_data.timezone import BRASILIA_TZ

    monkeypatch.setattr(
        execution_service,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "execution_live_capability_locked": False,
            },
        )(),
    )

    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="demo-desk", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "market_close"],
        timeframe="M5",
        day_trade=True,
    )

    deployment_detail = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="demo-deployment",
            broker_mode="mt5_live",
            live_activation_enabled=True,
            source_backtest_run_id=run.id,
        ),
    )
    deployment = execution_service.get_execution_deployment(api_db_session, deployment_detail.id)
    deployment.lifecycle = DeploymentLifecycle.RUNNING.value
    api_db_session.flush()

    clock = FixedClock(datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc))
    acquire_worker_lease(
        api_db_session,
        deployment_id=deployment.id,
        worker_id="worker-test",
        lease_token="lease-123",
        ttl_seconds=120,
        now=clock.now(),
    )

    strategy = build_research_strategy_from_compiled(deployment.compiled_config, session=api_db_session)
    identity = StrategyIdentity(
        strategy_name=deployment.strategy_name,
        strategy_version=deployment.strategy_version,
        compiled_config=deployment.compiled_config,
        config_hash=deployment.config_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        sizing_config=deployment.sizing_config,
        risk_config=deployment.risk_config,
    )
    evaluator = ResearchEvaluator(
        deployment_id=str(deployment.id),
        identity=identity,
        strategy_instance=strategy,
        window_bound=100,
    )

    with fake_edge_server() as (base_url, state):
        # Configure fake edge trade_mode as "demo" (Q-135 demo account rule)
        state.account_trade_mode = "demo"
        # Mock lookup outcomes for submitted orders
        state.default_lookup = lambda body: {
            "outcome": "filled",
            "closes_intent": True,
            "deals": [make_filled_lookup_deal(ticket=999, symbol="WIN$", volume=1.0, price=130000.0)],
        }

        quote_source, broker, ledger, service = _components(base_url, clock)

        # 1. Bar inside entry window (10:00 Brasilia) -> evaluates to BUY -> submits market entry
        bar_10_dt = pd.Timestamp("2024-06-03 10:00", tz=BRASILIA_TZ).to_pydatetime()
        bars_df = pd.DataFrame(
            {
                "open": [130000.0],
                "high": [130050.0],
                "low": [129950.0],
                "close": [130020.0],
                "volume": [1500.0],
            },
            index=pd.DatetimeIndex([bar_10_dt]),
        )
        eval_result = evaluator.ingest_completed_bar(bars_df)

        assert eval_result.signal_action.value == "buy"

        from q_backend.execution.brokers.base import PaperCostConfig

        cost_cfg = PaperCostConfig(
            point_value=Decimal("0.2"),
            slippage_points=Decimal("0"),
            cost_per_contract=Decimal("0"),
            cost_bps=Decimal("0"),
        )

        decision = service.process_completed_bar(
            api_db_session,
            deployment=deployment,
            eval_result=eval_result,
            lease_token="lease-123",
            worker_id="worker-test",
            cost_config=cost_cfg,
            point_value=Decimal("0.2"),
        )
        api_db_session.commit()

        assert decision.outcome == DecisionOutcome.ORDER_FILLED
        submits = [r for r in state.requests if r[1] == "/v1/submit"]
        assert len(submits) == 1

        # Position is now open
        pos = get_open_net_position(api_db_session, deployment.id)
        assert pos is not None
        assert pos.is_open

        # Update evaluator's trade model to reflect the open position
        evaluator.set_open_trade(
            Trade(
                id="t1",
                order_id="o1",
                symbol="WIN$",
                action=OrderAction.BUY,
                entry_time=bar_10_dt,
                entry_price=130000.0,
                quantity=1.0,
            )
        )

        # 2. Bar at day_trade_close_time (17:00 Brasilia) -> evaluator submits day-trade market close
        clock._now = datetime(2024, 6, 3, 20, 0, tzinfo=timezone.utc)
        bar_17_dt = pd.Timestamp("2024-06-03 17:00", tz=BRASILIA_TZ).to_pydatetime()
        bars_df_close = pd.DataFrame(
            {
                "open": [130100.0],
                "high": [130150.0],
                "low": [130050.0],
                "close": [130120.0],
                "volume": [2000.0],
            },
            index=pd.DatetimeIndex([bar_17_dt]),
        )
        eval_result_close = evaluator.ingest_completed_bar(bars_df_close)

        assert eval_result_close.signal_action.value == "close"
        assert eval_result_close.reason == "day_trade_close"

        decision_close = service.process_completed_bar(
            api_db_session,
            deployment=deployment,
            eval_result=eval_result_close,
            lease_token="lease-123",
            worker_id="worker-test",
            cost_config=cost_cfg,
            point_value=Decimal("0.2"),
        )
        api_db_session.commit()

        assert decision_close.outcome == DecisionOutcome.ORDER_FILLED
        submits = [r for r in state.requests if r[1] == "/v1/submit"]
        assert len(submits) == 2  # Both entry and day-trade close were submitted!


@pytest.mark.parametrize(
    "order_kind",
    [
        "protective_levels",
        "amend",
        "cancel",
        "resting_stop",
        "resting_limit",
        "oco",
        "timed_exit",
        "expiring_entry",
    ],
)
def test_create_deployment_admits_the_q137_order_kinds_and_paper_refuses_them(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
    order_kind: str,
):
    """The deployable-set gate admits the kind; the paper broker then refuses it at creation."""
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name=f"desk-{order_kind}", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", order_kind],
        timeframe="M5",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name=f"live-placeable-{order_kind}",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "paper deployments cannot place" in str(exc.value.detail)


@pytest.mark.parametrize("order_kind", ["priced_entry", "priced_close", "tick_hooks"])
def test_create_deployment_refuses_priced_orders_and_tick_hooks(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
    order_kind: str,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name=f"desk-{order_kind}", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", order_kind],
        timeframe="M5",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name=f"not-deployable-{order_kind}",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert order_kind in str(exc.value.detail)


def test_paper_deployment_refuses_resting_and_protective_orders(
    api_db_session: Session,
    frozen_version: ResearchStrategyVersion,
):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-paper-resting", initial_balance=Decimal("100000")),
    )
    run = _create_saved_run(
        api_db_session,
        frozen_version,
        order_kinds=["market_entry", "resting_stop"],
        timeframe="M5",
        day_trade=True,
    )

    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="paper-resting",
                source_backtest_run_id=run.id,
            ),
        )
    assert exc.value.status_code == 422
    assert "MT5 demo" in str(exc.value.detail)
