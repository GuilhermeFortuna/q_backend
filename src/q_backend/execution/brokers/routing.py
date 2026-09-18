"""Dispatch broker operations by deployment broker mode."""

from __future__ import annotations

from q_backend.execution.brokers.base import (
    BrokerHealth,
    BrokerOrderState,
    BrokerSubmissionResult,
    ExecutionBroker,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.domain import BrokerMode


class BrokerRouter:
    """Single ExecutionBroker facade over paper and live edge adapters."""

    def __init__(self, *, paper: ExecutionBroker, mt5_live: ExecutionBroker) -> None:
        self._paper = paper
        self._live = mt5_live

    def _broker_for(self, request: MarketOrderRequest) -> ExecutionBroker:
        if request.broker_mode == BrokerMode.MT5_LIVE:
            return self._live
        return self._paper

    def health(self) -> BrokerHealth:
        paper = self._paper.health()
        live = self._live.health()
        return BrokerHealth(
            broker_mode=BrokerMode.PAPER,
            is_available=paper.is_available or live.is_available,
            message=f"paper={paper.message}; live={live.message}",
            checked_at=paper.checked_at,
        )

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        return self._broker_for(request).submit_market_order(request, cost_config=cost_config)

    def lookup_order(self, request: MarketOrderRequest) -> BrokerOrderState:
        return self._broker_for(request).lookup_order(request)
