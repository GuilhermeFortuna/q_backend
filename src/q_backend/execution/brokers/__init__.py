"""Broker-neutral execution adapters."""

from q_backend.execution.brokers.base import (
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    ExecutionBroker,
)
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.brokers.metatrader import MetaTraderBroker
from q_backend.execution.brokers.paper import PaperBroker

__all__ = [
    "BrokerSubmissionOutcome",
    "BrokerSubmissionResult",
    "ExecutionBroker",
    "LiveExecutionGates",
    "MetaTraderBroker",
    "PaperBroker",
]
