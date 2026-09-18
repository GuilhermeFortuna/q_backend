import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    LedgerEntryType,
    PositionSide,
    RiskRejectionCode,
    SignalAction,
    StrategyIdentity,
)
from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.execution_repositories import (
    append_ledger_entry,
    create_execution_decision,
    create_execution_deployment,
    create_execution_fill,
    create_execution_order_intent,
    create_paper_account,
    record_risk_event,
    set_kill_switch,
    transition_deployment_lifecycle,
    transition_execution_order,
    upsert_open_net_position,
)
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.relay import OutboxRelay, RelayConfig
from q_backend.streaming.snapshot import EXECUTION_TOPICS, read_execution_snapshot


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="race_strategy",
        strategy_version=1,
        compiled_config={"threshold": 0.5},
        config_hash="race_hash",
        symbol="WIN$",
        timeframe="5m",
        sizing_config={"fixed_qty": 1.0},
        risk_config={"max_loss": 500.0},
    )


@pytest.mark.integration
def test_execution_snapshot_race_convergence_over_seeded_runs():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = get_binary_redis()
    relay = OutboxRelay(session_factory, client, RelayConfig(batch_size=1000))
    identity = _identity()

    num_seeds = int(os.getenv("Q_RACE_SEEDS", "200"))
    intermediate_count = 0
    trailing_count = 0

    for seed in range(num_seeds):
        rng = random.Random(seed)

        # 1. Reset database tables and outbox sequence counters
        with engine.connect() as conn:
            conn.execute(text("""
                TRUNCATE TABLE 
                    execution_risk_events, execution_ledger_entries, execution_fills,
                    execution_orders, execution_decisions, execution_net_positions,
                    execution_deployments, paper_accounts, execution_control_state,
                    stream_outbox
                CASCADE;
            """))
            conn.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0, last_relayed_seq = 0;"))
            conn.commit()

        # 2. Reset Redis execution topic streams
        client.delete(*(stream_key(t) for t in EXECUTION_TOPICS))

        # 3. Setup initial entities
        with session_factory() as session:
            acc = create_paper_account(session, name=f"acc-race-{seed}", initial_balance=Decimal("100000.00"))
            dep = create_execution_deployment(
                session, paper_account_id=acc.id, name=f"dep-race-{seed}", identity=identity
            )
            transition_deployment_lifecycle(session, dep.id, DeploymentLifecycle.RUNNING)
            session.commit()
            acc_id = acc.id
            dep_id = dep.id

        # 4. Drain initial outbox rows to Redis
        while relay.run_once() > 0:
            pass

        # 5. Client subscribes to stream (records current tail IDs)
        client_cursors = {}
        for topic in EXECUTION_TOPICS:
            sk = stream_key(topic)
            latest = client.xrevrange(sk, count=1)
            client_cursors[topic] = latest[0][0] if latest else "0-0"

        snapshot_holder: list[dict] = []

        def writer_actions():
            # Step A: change deployment lifecycle
            time.sleep(rng.uniform(0.0001, 0.002))
            with session_factory() as s:
                transition_deployment_lifecycle(s, dep_id, DeploymentLifecycle.PAUSED)
                s.commit()

            # Step B: create decision and order intent
            time.sleep(rng.uniform(0.0001, 0.002))
            with session_factory() as s:
                dec = create_execution_decision(
                    s,
                    deployment_id=dep_id,
                    bar_close_time=datetime.now(timezone.utc),
                    identity=identity,
                    signal_action=SignalAction.BUY,
                    outcome=DecisionOutcome.SIGNAL,
                    requested_quantity=Decimal("1.0"),
                )
                ord_intent = create_execution_order_intent(
                    s,
                    deployment_id=dep_id,
                    decision_id=dec.id,
                    broker_mode=BrokerMode.PAPER,
                    side=ExecutionSide.BUY,
                    quantity=Decimal("1.0"),
                )
                s.commit()
                order_id = ord_intent.id

            # Step C: transition order, fill, position, ledger
            time.sleep(rng.uniform(0.0001, 0.002))
            with session_factory() as s:
                transition_execution_order(s, order_id=order_id, target=ExecutionOrderStatus.SUBMITTED)
                upsert_open_net_position(
                    s,
                    deployment_id=dep_id,
                    side=PositionSide.LONG,
                    quantity=Decimal("1.0"),
                    average_entry_price=Decimal("125000.00"),
                    opened_at=datetime.now(timezone.utc),
                )
                create_execution_fill(
                    s,
                    deployment_id=dep_id,
                    order_id=order_id,
                    broker_mode=BrokerMode.PAPER,
                    external_fill_id=f"ext-fill-{seed}-{uuid.uuid4().hex[:6]}",
                    side=ExecutionSide.BUY,
                    quantity=Decimal("1.0"),
                    price=Decimal("125000.00"),
                    filled_at=datetime.now(timezone.utc),
                )
                append_ledger_entry(
                    s,
                    paper_account_id=acc_id,
                    deployment_id=dep_id,
                    entry_type=LedgerEntryType.FILL_CASH,
                    amount=Decimal("-25000.00"),
                    balance_after=Decimal("75000.00"),
                )
                s.commit()

            # Step D: risk event and kill switch
            time.sleep(rng.uniform(0.0001, 0.002))
            with session_factory() as s:
                record_risk_event(
                    s,
                    deployment_id=dep_id,
                    rejection_code=RiskRejectionCode.DAILY_LOSS_LIMIT,
                    message="Daily loss limit warning",
                )
                set_kill_switch(s, enabled=True, reason=f"Race seed {seed}", updated_by="admin")
                s.commit()

        thread = threading.Thread(target=writer_actions)
        thread.start()

        # Concurrently read snapshot with random jitter
        if seed % 3 == 0:
            # Trailing snapshot: sleep longer so writer commits complete first
            time.sleep(rng.uniform(0.030, 0.050))
        else:
            # Intermediate snapshot: read while writer is actively writing
            time.sleep(rng.uniform(0.0001, 0.006))
        snapshot_holder.append(read_execution_snapshot(session_factory))
        thread.join()

        # 6. Relay all outbox events to Redis
        while relay.run_once() > 0:
            pass

        # 7. Fetch all stream entries received after subscription
        snapshot = snapshot_holder[0]
        new_entries = {t: [] for t in EXECUTION_TOPICS}
        for topic in EXECUTION_TOPICS:
            sk = stream_key(topic)
            for sid, flds in client.xrange(sk, min=client_cursors[topic], max="+"):
                if sid == client_cursors[topic]:
                    continue
                env, _ = decode_entry(flds)
                new_entries[topic].append(env)

        # 8. Client applies §4.2:
        # Initialize client state from snapshot
        c_deployments = {d["id"]: d for d in snapshot["deployments"]}
        c_accounts = {a["id"]: a for a in snapshot["accounts"]}
        c_positions = {p["deployment_id"]: p for p in snapshot["positions"]}
        c_orders = {o["id"]: o for o in snapshot["orders"]}
        c_control = dict(snapshot["control"])

        # Discard entries with seq <= watermark[topic], apply remaining in seq order
        for topic in EXECUTION_TOPICS:
            watermark_seq = snapshot["watermark"][topic]["seq"]
            for env in new_entries[topic]:
                if env.seq <= watermark_seq:
                    continue
                payload = env.payload
                if topic == "deployments":
                    c_deployments[payload["id"]] = payload
                elif topic == "orders":
                    c_orders[payload["id"]] = payload
                elif topic == "fills" and payload.get("position_after"):
                    pos = payload["position_after"]
                    if pos.get("is_open"):
                        c_positions[payload["deployment_id"]] = pos
                    else:
                        c_positions.pop(payload["deployment_id"], None)
                elif topic == "ledger" and payload.get("account_after"):
                    c_accounts[payload["account_id"]] = payload["account_after"]
                elif topic == "risk" and "kill_switch_enabled" in payload:
                    c_control["kill_switch_enabled"] = payload["kill_switch_enabled"]
                    c_control["kill_switch_reason"] = payload.get("kill_switch_reason")
                    c_control["updated_by"] = payload.get("updated_by")

        # 9. Fresh snapshot taken after all writes completed
        final_snapshot = read_execution_snapshot(session_factory)

        # Track race conditions
        if snapshot["watermark"]["orders"]["seq"] < final_snapshot["watermark"]["orders"]["seq"]:
            intermediate_count += 1
        else:
            trailing_count += 1

        # 10. Assert client state equals final snapshot state
        # Deployments
        final_deps = {d["id"]: d for d in final_snapshot["deployments"]}
        assert {k: v["lifecycle"] for k, v in c_deployments.items()} == {
            k: v["lifecycle"] for k, v in final_deps.items()
        }

        # Orders
        final_orders = {o["id"]: o for o in final_snapshot["orders"]}
        assert {k: v["status"] for k, v in c_orders.items()} == {k: v["status"] for k, v in final_orders.items()}

        # Positions
        final_positions = {p["deployment_id"]: p for p in final_snapshot["positions"]}
        assert {k: v["quantity"] for k, v in c_positions.items()} == {
            k: v["quantity"] for k, v in final_positions.items()
        }

        # Accounts
        final_accounts = {a["id"]: a for a in final_snapshot["accounts"]}
        assert {k: v["cash_balance"] for k, v in c_accounts.items()} == {
            k: v["cash_balance"] for k, v in final_accounts.items()
        }

        # Control
        assert c_control["kill_switch_enabled"] == final_snapshot["control"]["kill_switch_enabled"]
        assert c_control["kill_switch_reason"] == final_snapshot["control"]["kill_switch_reason"]

    # Assert both intermediate and trailing races were exercised across the run
    assert intermediate_count > 0, "Expected at least one intermediate race snapshot"
    assert trailing_count > 0, "Expected at least one trailing race snapshot"
