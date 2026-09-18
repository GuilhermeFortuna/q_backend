"""Standalone forward execution worker CLI (not Dramatiq, not API lifespan)."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Generator

import sentry_sdk

from q_backend.execution.bar_coordinator import BarCoordinator
from q_backend.execution.brokers.base import ExecutionBroker, PaperCostConfig, QuoteSource
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.brokers.routing import BrokerRouter
from q_backend.execution.commands import get_deployment_or_raise
from q_backend.execution.domain import DecisionOutcome
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.quote_source import EdgeQuoteSource
from q_backend.execution.recovery import ExecutionRecovery
from q_backend.execution.service import CrashInjector, ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.market_data.service import MarketDataService
from q_backend.observability.sentry import init_sentry
from q_backend.storage.db.engine import create_session_factory, session_scope
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    clear_pending_deployment_action,
    release_worker_lease,
)
from q_backend.storage.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_CRASH_CHECKPOINTS = frozenset(
    {
        "before_intent_commit",
        "after_intent_commit",
        "after_broker_response",
        "before_fill_commit",
    }
)


class _LiveClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _ExecutionComponents:
    market_data: MarketDataService
    quote_source: QuoteSource
    broker: ExecutionBroker
    ledger: ExecutionLedger
    service: ExecutionService
    coordinator: BarCoordinator
    recovery: ExecutionRecovery
    settings: Settings
    clock: _LiveClock


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@contextmanager
def _market_data_session() -> Generator[MarketDataService, None, None]:
    """Initialize MT5 once for a CLI command and always shut it down."""
    md = MarketDataService()
    if not md.initialize():
        logger.warning("MT5 initialization returned False; using local/fallback data paths")
    try:
        yield md
    finally:
        try:
            md.shutdown()
        except Exception:  # noqa: BLE001 - best-effort MT5 shutdown; logged
            logger.debug("MT5 shutdown raised", exc_info=True)


def _paper_cost_config(settings: Settings) -> PaperCostConfig:
    return PaperCostConfig(
        point_value=Decimal(str(settings.execution_default_point_value)),
        slippage_points=Decimal(str(settings.execution_paper_slippage_points)),
        cost_per_contract=Decimal(str(settings.execution_paper_cost_per_contract)),
        cost_bps=Decimal(str(settings.execution_paper_cost_bps)),
        max_quote_age_seconds=settings.execution_max_quote_age_seconds,
    )


def _edge_client(settings: Settings) -> EdgeClient:
    return EdgeClient(
        settings.mt5_edge_url,
        timeouts=EdgeTimeouts(
            connect_s=settings.mt5_edge_connect_timeout_s,
            read_s=settings.mt5_edge_read_timeout_s,
            submit_read_s=settings.mt5_edge_submit_timeout_s,
        ),
    )


def _live_gates(settings: Settings) -> LiveExecutionGates:
    return LiveExecutionGates.from_settings(
        enabled=settings.live_execution_enabled,
        account_allowlist=settings.live_execution_account_allowlist,
        deployment_live_activation_enabled=False,
        controlled_account_validated=settings.live_execution_validated,
    )


def _build_components(
    market_data: MarketDataService,
    *,
    settings: Settings | None = None,
    crash_injector: CrashInjector | None = None,
) -> _ExecutionComponents:
    settings = settings or get_settings()
    clock = _LiveClock()
    edge_client = _edge_client(settings)
    quote_source = EdgeQuoteSource(client=edge_client, clock=clock.now)
    paper_broker = PaperBroker(quote_source=quote_source, clock=clock)
    live_broker = EdgeBroker(
        client=edge_client,
        clock=clock,
        gates=_live_gates(settings),
        slippage_deviation=settings.live_execution_slippage_deviation,
        lookup_window_lead_s=settings.execution_lookup_window_lead_s,
    )
    broker = BrokerRouter(paper=paper_broker, mt5_live=live_broker)
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quote_source,
        ledger=ledger,
        crash_injector=crash_injector,
        max_bar_age_seconds=settings.execution_max_bar_age_seconds,
        clock=clock.now,
    )
    coordinator = BarCoordinator(provider=market_data, clock=clock.now)
    recovery = ExecutionRecovery(
        coordinator=coordinator,
        quote_source=quote_source,
        ledger=ledger,
        point_value=Decimal(str(settings.execution_default_point_value)),
        initial_window_bars=settings.execution_initial_window_bars,
    )
    return _ExecutionComponents(
        market_data=market_data,
        quote_source=quote_source,
        broker=broker,
        ledger=ledger,
        service=service,
        coordinator=coordinator,
        recovery=recovery,
        settings=settings,
        clock=clock,
    )


def _build_worker(
    components: _ExecutionComponents,
    *,
    poll_interval_seconds: float | None = None,
) -> ExecutionWorker:
    return ExecutionWorker(
        session_factory=create_session_factory(),
        coordinator=components.coordinator,
        quote_source=components.quote_source,
        broker=components.broker,
        ledger=components.ledger,
        service=components.service,
        recovery=components.recovery,
        settings=components.settings,
        clock=components.clock.now,
        poll_interval_seconds=poll_interval_seconds,
    )


def _parse_deployment_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid deployment UUID: {raw}") from exc


def _validate_crash_at(crash_at: str | None, log_level: str) -> CrashInjector | None:
    if crash_at is None:
        return None
    if crash_at not in _CRASH_CHECKPOINTS:
        raise SystemExit(f"invalid --crash-at checkpoint: {crash_at}")
    if log_level != "INFO":
        raise SystemExit("--crash-at requires --log-level INFO so the banner is visible")
    logger.warning(
        "VALIDATION ONLY: crash checkpoint %s is enabled; process will exit at that point",
        crash_at,
    )
    return CrashInjector(checkpoints={crash_at})


def cmd_run(args: argparse.Namespace) -> int:
    crash_injector = _validate_crash_at(args.crash_at, args.log_level)
    with _market_data_session() as md:
        components = _build_components(md, crash_injector=crash_injector)
        worker = _build_worker(
            components,
            poll_interval_seconds=args.poll_interval,
        )
        logger.info(
            "starting execution worker id=%s poll_interval=%ss edge=%s",
            components.settings.execution_worker_id,
            args.poll_interval or components.settings.execution_poll_interval_seconds,
            components.settings.mt5_edge_url,
        )
        worker.run()
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    deployment_id: uuid.UUID = args.deployment_id
    settings = get_settings()
    cost_config = _paper_cost_config(settings)
    point_value = Decimal(str(settings.execution_default_point_value))
    lease_token = str(uuid.uuid4())

    with _market_data_session() as md:
        components = _build_components(md, settings=settings)
        with session_scope() as session:
            deployment = get_deployment_or_raise(session, deployment_id)
            acquire_worker_lease(
                session,
                deployment_id=deployment.id,
                worker_id=settings.execution_worker_id,
                lease_token=lease_token,
                ttl_seconds=settings.execution_lease_ttl_seconds,
            )
            try:
                result = components.service.flatten_deployment(
                    session,
                    deployment=deployment,
                    lease_token=lease_token,
                    worker_id=settings.execution_worker_id,
                    cost_config=cost_config,
                    point_value=point_value,
                )
                clear_pending_deployment_action(session, deployment.id)
            finally:
                try:
                    release_worker_lease(
                        session,
                        deployment_id=deployment.id,
                        worker_id=settings.execution_worker_id,
                        lease_token=lease_token,
                    )
                except ValueError:
                    logger.warning(
                        "lease release mismatch during flatten for deployment %s",
                        deployment.id,
                    )

    logger.info(
        "flatten deployment=%s outcome=%s timing=%s",
        deployment_id,
        result.outcome.value,
        result.timing.to_log_dict(),
    )
    if result.outcome in {
        DecisionOutcome.ORDER_FILLED,
        DecisionOutcome.HOLD,
    }:
        return 0
    logger.error("flatten did not complete successfully: outcome=%s", result.outcome.value)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Q forward execution worker")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run the execution worker loop until SIGINT/SIGTERM")
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Override Q_EXECUTION_POLL_INTERVAL_SECONDS for this process",
    )
    run_parser.add_argument(
        "--crash-at",
        choices=sorted(_CRASH_CHECKPOINTS),
        default=None,
        help="Validation-only crash checkpoint (requires --log-level INFO)",
    )
    run_parser.set_defaults(func=cmd_run)

    flatten_parser = sub.add_parser(
        "flatten",
        help="Flatten one deployment (bypasses strategy signals, not lease or persistence safety)",
    )
    flatten_parser.add_argument(
        "deployment_id",
        type=_parse_deployment_id,
        help="Deployment UUID",
    )
    flatten_parser.set_defaults(func=cmd_flatten)
    return parser


def main(argv: list[str] | None = None) -> None:
    init_sentry(get_settings(), component="cli")
    sentry_sdk.set_tag("cli_command", "q_execution")
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    code = args.func(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
