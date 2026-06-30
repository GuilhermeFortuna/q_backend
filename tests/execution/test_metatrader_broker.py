"""Contract tests for the locked MT5 live broker adapter (WO172)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from q_backend.execution.brokers.base import (
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    PaperCostConfig,
)
from q_backend.execution.brokers.live_gates import LiveExecutionGates, evaluate_live_gates
from q_backend.execution.brokers.metatrader import (
    MetaTraderBroker,
    intent_comment,
    intent_magic,
    normalize_price,
    normalize_volume,
    select_filling_mode,
)
from q_backend.execution.brokers.mt5_constants import (
    Mt5OrderFilling,
    Mt5Retcode,
    Mt5SymbolFilling,
)
from q_backend.execution.domain import ExecutionSide
from tests.execution.fake_mt5_runtime import (
    FakeMt5Runtime,
    FakeSendResult,
    market_request,
    open_gates,
    seed_symbol,
)
from q_backend.execution.brokers.fakes import FixedClock


@pytest.fixture
def cost_config() -> PaperCostConfig:
    return PaperCostConfig(point_value=Decimal("0.2"))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))


def _broker(
    runtime: FakeMt5Runtime,
    clock: FixedClock,
    *,
    dry_run: bool = False,
    gates: LiveExecutionGates | None = None,
) -> MetaTraderBroker:
    runtime.now_fn = clock.now
    tick_time = int(clock.now().timestamp())
    seed_symbol(runtime, "WIN$", tick_time=tick_time)
    seed_symbol(
        runtime,
        "WDO$",
        filling_mode=int(Mt5SymbolFilling.FOK),
        tick_time=tick_time,
    )
    return MetaTraderBroker(
        runtime=runtime,
        clock=clock,
        gates=gates or open_gates(),
        dry_run=dry_run,
    )


@pytest.mark.parametrize(
    "gates,account_login,expected_code",
    [
        (
            LiveExecutionGates(enabled=False),
            12345678,
            BrokerRejectionCode.LIVE_LOCKED,
        ),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({999}),
                deployment_live_activation_enabled=True,
                controlled_account_validated=True,
            ),
            12345678,
            BrokerRejectionCode.ACCOUNT_NOT_ALLOWLISTED,
        ),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({12345678}),
                deployment_live_activation_enabled=False,
                controlled_account_validated=True,
            ),
            12345678,
            BrokerRejectionCode.LIVE_LOCKED,
        ),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({12345678}),
                deployment_live_activation_enabled=True,
                controlled_account_validated=False,
            ),
            12345678,
            BrokerRejectionCode.LIVE_LOCKED,
        ),
    ],
)
def test_activation_gate_matrix(gates, account_login, expected_code):
    rejection = evaluate_live_gates(gates, account_login=account_login)
    assert rejection is not None
    assert rejection.code == expected_code


def test_buy_and_sell_translation(clock, cost_config):
    runtime = FakeMt5Runtime()
    broker = _broker(runtime, clock)
    order_id = uuid4()
    buy = broker.submit_market_order(
        market_request(order_id=order_id, side="buy"),
        cost_config=cost_config,
    )
    assert buy.outcome == BrokerSubmissionOutcome.FILLED
    assert runtime.orders[-1]["type"] == 0
    assert runtime.orders[-1]["volume"] == 1.0

    sell = broker.submit_market_order(
        market_request(order_id=uuid4(), side="sell"),
        cost_config=cost_config,
    )
    assert sell.outcome == BrokerSubmissionOutcome.FILLED
    assert runtime.orders[-1]["type"] == 1


@pytest.mark.parametrize(
    "filling_mode,expected",
    [
        (int(Mt5SymbolFilling.FOK), int(Mt5OrderFilling.FOK)),
        (int(Mt5SymbolFilling.IOC), int(Mt5OrderFilling.IOC)),
        (int(Mt5SymbolFilling.RETURN), int(Mt5OrderFilling.RETURN)),
    ],
)
def test_select_filling_mode_from_symbol(filling_mode, expected):
    assert select_filling_mode(filling_mode) == expected


def test_volume_and_price_normalization():
    assert normalize_volume(Decimal("3"), step=1.0, vmin=1.0, vmax=10.0) == 3.0
    assert normalize_price(100.4567, 2) == 100.46
    with pytest.raises(ValueError):
        normalize_volume(Decimal("0.5"), step=1.0, vmin=1.0, vmax=10.0)


def test_unsupported_symbol_rejected(clock, cost_config):
    runtime = FakeMt5Runtime()
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4(), symbol="MISSING"),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.SYMBOL_UNAVAILABLE


def test_order_check_failure(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.check_retcode = int(Mt5Retcode.REJECT)
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.ORDER_CHECK_FAILED


def test_order_send_rejection(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.send_retcode = int(Mt5Retcode.REJECT)
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.ORDER_SEND_FAILED


def test_order_send_none_is_unknown_and_recovers_existing_deal(clock, cost_config):
    runtime = FakeMt5Runtime()
    order_id = uuid4()
    magic = intent_magic(order_id)
    comment = intent_comment(order_id)
    from tests.execution.fake_mt5_runtime import FakeDeal

    runtime.deals = [
        FakeDeal(
            ticket=555,
            magic=magic,
            comment=comment,
            volume=1.0,
            price=100.5,
            time=int(clock.now().timestamp()),
        )
    ]
    runtime.send_returns_none = True
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=order_id),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.FILLED
    assert result.metadata.get("recovered") is True


def test_unknown_outcome_when_no_deal_found(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.send_returns_none = True
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.UNKNOWN


def test_dry_run_blocks_order_send_even_when_gates_open(clock, cost_config):
    runtime = FakeMt5Runtime()
    broker = _broker(runtime, clock, dry_run=True)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.LIVE_LOCKED
    assert runtime.orders == []


def test_intent_identity_in_magic_and_comment():
    order_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert intent_comment(order_id).startswith("q:")
    assert intent_magic(order_id) > 0


def test_external_deal_deduplication(clock, cost_config):
    runtime = FakeMt5Runtime()
    order_id = uuid4()
    magic = intent_magic(order_id)
    comment = intent_comment(order_id)
    from tests.execution.fake_mt5_runtime import FakeDeal

    runtime.deals = [
        FakeDeal(
            ticket=1,
            magic=magic,
            comment=comment,
            volume=1.0,
            price=100.0,
            time=int(clock.now().timestamp()),
        ),
        FakeDeal(
            ticket=1,
            magic=magic,
            comment=comment,
            volume=1.0,
            price=100.0,
            time=int(clock.now().timestamp()),
        ),
    ]
    broker = _broker(runtime, clock)
    recovered = broker.recover_unknown(market_request(order_id=order_id), now=clock.now())
    assert recovered.outcome == BrokerSubmissionOutcome.FILLED
    assert len(recovered.external_deal_ids) == 1


def test_partial_retcode_reconciles_when_deal_present(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.send_result = FakeSendResult(
        retcode=int(Mt5Retcode.DONE_PARTIAL),
        order=9010,
        deal=0,
    )
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.FILLED


def test_trading_disabled_rejected(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.account.trade_allowed = False
    broker = _broker(runtime, clock)
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.rejection.code == BrokerRejectionCode.TRADING_DISABLED


def test_stub_health_reports_unavailable(clock):
    runtime = FakeMt5Runtime()

    class StubFlagRuntime(FakeMt5Runtime):
        def is_stub(self) -> bool:
            return True

    broker = MetaTraderBroker(
        runtime=StubFlagRuntime(),
        clock=clock,
        gates=open_gates(),
    )
    health = broker.health()
    assert health.is_available is False


def test_default_gates_deny_submission(clock, cost_config):
    runtime = FakeMt5Runtime()
    runtime.now_fn = clock.now
    seed_symbol(runtime, "WIN$", tick_time=int(clock.now().timestamp()))
    broker = MetaTraderBroker(
        runtime=runtime,
        clock=clock,
        gates=LiveExecutionGates(),
        dry_run=False,
    )
    result = broker.submit_market_order(
        market_request(order_id=uuid4()),
        cost_config=cost_config,
    )
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.LIVE_LOCKED
    runtime = FakeMt5Runtime()

    class StubFlagRuntime(FakeMt5Runtime):
        def is_stub(self) -> bool:
            return True

    broker = MetaTraderBroker(
        runtime=StubFlagRuntime(),
        clock=clock,
        gates=open_gates(),
    )
    health = broker.health()
    assert health.is_available is False
