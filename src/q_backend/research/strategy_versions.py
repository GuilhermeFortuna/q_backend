"""Immutable strategy versions: freezing a validated bundle, and reading it back.

Every backtest of a strategy file freezes the file's current bundle before it runs. A bundle whose
hash already has a version resolves to that version, so a version and every run that references it
stay the same when the file is edited or deleted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from q_backend.research.params import (
    coerce_params,
    declared_from_wire,
    to_wire_spec,
    wire_value,
)
from q_backend.research.strategy_files import (
    DEFAULT_TIMEOUT_S,
    BundleFile,
    StrategyBundle,
    StrategyFileError,
    StrategyFileInfo,
    build_bundle,
    bundle_from_files,
    validate_bundle,
)
from q_backend.storage.db.models import ResearchStrategyVersion


class StrategyFileRequestError(ValueError):
    """A strategy-file backtest request asks for something the stack does not run for strategy files."""


class StrategyVersionNotFound(LookupError):
    """No strategy version has the requested id."""


@dataclass(frozen=True)
class ResolvedStrategyRun:
    """A strategy-file run with its version frozen and its parameters coerced to their wire values."""

    strategy_id: str
    version_id: str
    bundle_hash: str
    strategy_params: dict[str, Any]


def version_by_hash(session: Session, bundle_hash: str) -> ResearchStrategyVersion | None:
    return session.scalar(select(ResearchStrategyVersion).where(ResearchStrategyVersion.bundle_hash == bundle_hash))


def freeze_bundle(session: Session, bundle: StrategyBundle, info: StrategyFileInfo) -> ResearchStrategyVersion:
    """Return the version of ``bundle``, creating it from the validated ``info`` when its hash is new.

    Two writers may freeze the same new bundle at once. The loser's insert hits the unique hash
    constraint inside a savepoint, and it returns the row the winner wrote.
    """
    if not info.valid or info.class_name is None:
        raise StrategyFileError(f"'{bundle.strategy_id}' is not a valid strategy file: {info.error}")
    existing = version_by_hash(session, bundle.bundle_hash)
    if existing is not None:
        return existing

    row = ResearchStrategyVersion(
        id=uuid.uuid4(),
        strategy_id=bundle.strategy_id,
        class_name=info.class_name,
        bundle_hash=bundle.bundle_hash,
        source_files=[{"path": file.path, "content": file.content} for file in bundle.files],
        param_specs=[to_wire_spec(param) for param in info.parameters],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        existing = version_by_hash(session, bundle.bundle_hash)
        if existing is None:
            raise
        return existing
    return row


def freeze_strategy_file(
    session: Session,
    root: Path,
    strategy_id: str,
    *,
    bundle_root: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ResearchStrategyVersion:
    """Freeze the strategy file's current bundle, validating it first when its hash is new."""
    bundle = build_bundle(root, strategy_id)
    existing = version_by_hash(session, bundle.bundle_hash)
    if existing is not None:
        return existing
    info = validate_bundle(bundle, bundle_root, timeout_s=timeout_s)
    return freeze_bundle(session, bundle, info)


def bundle_of(version: ResearchStrategyVersion) -> StrategyBundle:
    """The version's frozen bundle. Raises when its stored source no longer matches its hash."""
    bundle = bundle_from_files(
        version.strategy_id,
        (BundleFile(path=entry["path"], content=entry["content"]) for entry in version.source_files),
    )
    if bundle.bundle_hash != version.bundle_hash:
        raise StrategyFileError(f"stored source of version {version.id} does not match its bundle hash")
    return bundle


def strategy_version_summaries(session: Session, version_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
    """(strategy id, bundle hash) of the given versions, read in one query for history listings."""
    if not version_ids:
        return {}
    rows = session.execute(
        select(
            ResearchStrategyVersion.id, ResearchStrategyVersion.strategy_id, ResearchStrategyVersion.bundle_hash
        ).where(ResearchStrategyVersion.id.in_(version_ids))
    )
    return {row.id: (row.strategy_id, row.bundle_hash) for row in rows}


def check_strategy_run_request(
    *,
    engine: str,
    ml_filter: object | None,
    position_sizing: Any | None,
) -> int:
    """Refuse what a strategy-file run does not support; return the fixed order quantity."""
    if engine != "candle":
        raise StrategyFileRequestError("Strategy files run on the candle engine only.")
    if ml_filter is not None:
        raise StrategyFileRequestError("ML filters do not apply to strategy files.")
    if position_sizing is None:
        return 1
    if getattr(position_sizing, "type", None) != "fixed_quantity":
        raise StrategyFileRequestError("Strategy files support fixed-quantity position sizing only.")
    quantity = float(position_sizing.quantity)
    if not quantity.is_integer() or quantity < 1:
        raise StrategyFileRequestError("Strategy file quantity must be a positive whole number.")
    return int(quantity)


def resolve_strategy_run(
    session: Session,
    *,
    strategy_file: str | None,
    strategy_version_id: str | None,
    strategy_params: Mapping[str, Any],
    root: Path,
    bundle_root: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ResolvedStrategyRun:
    """Freeze the file's current version, or load the requested one, and coerce the run's parameters.

    Raises :class:`StrategyFileError` for a file that cannot run, :class:`ParamError` for parameters
    that do not match the version's declaration, and :class:`StrategyVersionNotFound` for an unknown id.
    """
    if strategy_version_id is not None:
        version = _version_by_id(session, strategy_version_id)
        if version is None:
            raise StrategyVersionNotFound(f"Strategy version '{strategy_version_id}' not found.")
    else:
        version = freeze_strategy_file(session, root, strategy_file or "", bundle_root=bundle_root, timeout_s=timeout_s)

    declared = tuple(declared_from_wire(spec) for spec in version.param_specs)
    values = coerce_params(declared, strategy_params)
    return ResolvedStrategyRun(
        strategy_id=version.strategy_id,
        version_id=str(version.id),
        bundle_hash=version.bundle_hash,
        strategy_params={param.name: wire_value(param.kind, values[param.name]) for param in declared},
    )


def _version_by_id(session: Session, version_id: str) -> ResearchStrategyVersion | None:
    try:
        parsed = uuid.UUID(version_id)
    except ValueError:
        return None
    return session.get(ResearchStrategyVersion, parsed)
