"""Lock-box holdout consumption records (WO163)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from q_backend.storage.lake.artifacts import (
    read_lockbox_consumption as _read_record,
    write_lockbox_consumption as _write_record,
)


class HoldoutConsumedError(Exception):
    """Raised when a modified champion attempts to reuse a consumed holdout."""

    def __init__(self, manifest_hash: str, existing_champion_hash: str):
        self.manifest_hash = manifest_hash
        self.existing_champion_hash = existing_champion_hash
        super().__init__(
            "Lock-box holdout for split manifest "
            f"'{manifest_hash}' was already consumed by champion "
            f"'{existing_champion_hash[:12]}…'. "
            "A modified candidate cannot be re-evaluated against the same tail."
        )


class LockboxConsumptionState(str, Enum):
    AVAILABLE = "available"
    SAME_CHAMPION = "same_champion"
    MANIFEST_CONSUMED = "manifest_consumed"


@dataclass(frozen=True)
class LockboxConsumptionRecord:
    manifest_hash: str
    champion_hash: str
    consumed_at: str
    lockbox_metrics: dict[str, Any]
    verdict: str
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "champion_hash": self.champion_hash,
            "consumed_at": self.consumed_at,
            "lockbox_metrics": self.lockbox_metrics,
            "verdict": self.verdict,
            "candidate_id": self.candidate_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LockboxConsumptionRecord:
        return cls(
            manifest_hash=str(payload["manifest_hash"]),
            champion_hash=str(payload["champion_hash"]),
            consumed_at=str(payload["consumed_at"]),
            lockbox_metrics=dict(payload.get("lockbox_metrics") or {}),
            verdict=str(payload.get("verdict", "")),
            candidate_id=payload.get("candidate_id"),
        )


@dataclass(frozen=True)
class LockboxConsumptionCheck:
    state: LockboxConsumptionState
    record: LockboxConsumptionRecord | None = None


def check_lockbox_consumption(
    manifest_hash: str,
    champion_hash: str,
) -> LockboxConsumptionCheck:
    """Determine whether the holdout may be evaluated for this champion."""
    try:
        payload = _read_record(manifest_hash)
    except FileNotFoundError:
        return LockboxConsumptionCheck(state=LockboxConsumptionState.AVAILABLE)

    record = LockboxConsumptionRecord.from_dict(payload)
    if record.champion_hash == champion_hash:
        return LockboxConsumptionCheck(
            state=LockboxConsumptionState.SAME_CHAMPION,
            record=record,
        )
    return LockboxConsumptionCheck(
        state=LockboxConsumptionState.MANIFEST_CONSUMED,
        record=record,
    )


def assert_lockbox_available(manifest_hash: str, champion_hash: str) -> LockboxConsumptionRecord | None:
    """Refuse modified-champion reuse; return cached record for idempotent re-read."""
    check = check_lockbox_consumption(manifest_hash, champion_hash)
    if check.state == LockboxConsumptionState.MANIFEST_CONSUMED:
        assert check.record is not None
        raise HoldoutConsumedError(manifest_hash, check.record.champion_hash)
    return check.record


def record_lockbox_consumption(
    *,
    manifest_hash: str,
    champion_hash: str,
    lockbox_metrics: dict[str, Any],
    verdict: str,
    candidate_id: str | None = None,
) -> LockboxConsumptionRecord:
    """Persist a one-shot holdout consumption record keyed by split manifest."""
    existing = check_lockbox_consumption(manifest_hash, champion_hash)
    if existing.state == LockboxConsumptionState.MANIFEST_CONSUMED:
        assert existing.record is not None
        raise HoldoutConsumedError(manifest_hash, existing.record.champion_hash)
    if existing.state == LockboxConsumptionState.SAME_CHAMPION and existing.record:
        return existing.record

    record = LockboxConsumptionRecord(
        manifest_hash=manifest_hash,
        champion_hash=champion_hash,
        consumed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        lockbox_metrics=lockbox_metrics,
        verdict=verdict,
        candidate_id=candidate_id,
    )
    _write_record(manifest_hash, record.to_dict())
    return record
