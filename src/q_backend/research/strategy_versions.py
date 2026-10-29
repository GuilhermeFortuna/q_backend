"""Immutable strategy versions: freezing a validated bundle, and reading it back.

Every backtest of a strategy file freezes the file's current bundle before it runs. A bundle whose
hash already has a version resolves to that version, so a version and every run that references it
stay the same when the file is edited or deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from q_backend.research.params import to_wire_spec
from q_backend.research.strategy_files import (
    BundleFile,
    StrategyBundle,
    StrategyFileError,
    StrategyFileInfo,
    build_bundle,
    bundle_from_files,
    validate_bundle,
    DEFAULT_TIMEOUT_S,
)
from q_backend.storage.db.models import ResearchStrategyVersion


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
