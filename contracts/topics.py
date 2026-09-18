# GENERATED FILE - DO NOT EDIT. Source schemas: schema/stream/topics.yaml
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TopicPolicy:
    name: str
    topic_class: Literal["durable", "ephemeral"]
    retention_duration: str
    retention_entries: int
    coalesce_key: tuple[str, ...]
    on_overflow: Literal["lag", "coalesce"]
    replay: Literal["unbounded", "retention_only"]
    payload_schema: str


TOPICS: Mapping[str, TopicPolicy] = {
    "bars.completed": TopicPolicy(
        name="bars.completed",
        topic_class="ephemeral",
        retention_duration="P1D",
        retention_entries=100000,
        coalesce_key=(),
        on_overflow="lag",
        replay="retention_only",
        payload_schema="schema/api/arrow/bars.schema.json",
    ),
    "bars.forming": TopicPolicy(
        name="bars.forming",
        topic_class="ephemeral",
        retention_duration="PT1H",
        retention_entries=10000,
        coalesce_key=("symbol", "timeframe"),
        on_overflow="coalesce",
        replay="retention_only",
        payload_schema="schema/api/arrow/bars.schema.json",
    ),
    "decisions": TopicPolicy(
        name="decisions",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-decision.schema.json",
    ),
    "deployments": TopicPolicy(
        name="deployments",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-deployment.schema.json",
    ),
    "fills": TopicPolicy(
        name="fills",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-fill.schema.json",
    ),
    "jobs.progress": TopicPolicy(
        name="jobs.progress",
        topic_class="ephemeral",
        retention_duration="P1D",
        retention_entries=50000,
        coalesce_key=("kind", "job_id"),
        on_overflow="coalesce",
        replay="retention_only",
        payload_schema="schema/stream/payloads/job-progress.schema.json",
    ),
    "jobs.terminal": TopicPolicy(
        name="jobs.terminal",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/job-terminal.schema.json",
    ),
    "ledger": TopicPolicy(
        name="ledger",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-ledger.schema.json",
    ),
    "orders": TopicPolicy(
        name="orders",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-order.schema.json",
    ),
    "quotes": TopicPolicy(
        name="quotes",
        topic_class="ephemeral",
        retention_duration="PT1H",
        retention_entries=100000,
        coalesce_key=("symbol",),
        on_overflow="coalesce",
        replay="retention_only",
        payload_schema="schema/api/arrow/ticks.schema.json",
    ),
    "risk": TopicPolicy(
        name="risk",
        topic_class="durable",
        retention_duration="P1D",
        retention_entries=200000,
        coalesce_key=(),
        on_overflow="lag",
        replay="unbounded",
        payload_schema="schema/stream/payloads/execution-risk.schema.json",
    ),
}
