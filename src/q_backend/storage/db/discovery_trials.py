"""Durable, paginated Discovery trials; the run owns their lifetime."""

import uuid
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from q_backend.storage.db.models import StrategySearchCandidate, StrategySearchTrial


def save_trial(session: Session, run_id: uuid.UUID, payload: dict[str, Any]) -> None:
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    values = {"id": uuid.uuid4(), "run_id": run_id, **payload}
    statement = insert(StrategySearchTrial).values(**values)
    identity = ["run_id", "candidate_id", "generation", "window_index", "trial_number"]
    statement = statement.on_conflict_do_update(
        index_elements=identity,
        set_={key: value for key, value in payload.items() if key not in identity},
    )
    session.execute(statement)


def save_candidate(session: Session, *, run_id: uuid.UUID, **payload: Any) -> None:
    """Upsert partials and final ranks under the same durable candidate identity."""
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    columns = set(StrategySearchCandidate.__table__.columns.keys()) - {"id", "run_id", "created_at", "updated_at"}
    values = {key: value for key, value in payload.items() if key in columns}
    statement = insert(StrategySearchCandidate).values(id=uuid.uuid4(), run_id=run_id, **values)
    session.execute(statement.on_conflict_do_update(index_elements=["run_id", "candidate_id"], set_=values))


def list_trials(
    session: Session,
    run_id: uuid.UUID,
    *,
    limit: int = 100,
    offset: int = 0,
    candidate_id: str | None = None,
    generation: int | None = None,
    window_index: int | None = None,
    state: str | None = None,
    objective_min: float | None = None,
    objective_max: float | None = None,
    q: str | None = None,
    sort: str = "finished_at",
    descending: bool = True,
) -> dict[str, Any]:
    model = StrategySearchTrial
    filters = [model.run_id == run_id]
    for key, value in dict(
        candidate_id=candidate_id, generation=generation, window_index=window_index, state=state
    ).items():
        if value is not None:
            filters.append(getattr(model, key) == value)
    if objective_min is not None:
        filters.append(model.objective_value >= objective_min)
    if objective_max is not None:
        filters.append(model.objective_value <= objective_max)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(cast(model.parameters, String).ilike(f"%{escaped}%", escape="\\"))
    total = session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
    column = getattr(model, sort)
    order = (column.desc() if descending else column.asc()).nulls_last()
    rows = session.scalars(select(model).where(*filters).order_by(order, model.id).limit(limit).offset(offset)).all()
    keys = [
        "candidate_id",
        "generation",
        "window_index",
        "trial_number",
        "state",
        "objective_value",
        "parameters",
        "metrics",
        "started_at",
        "finished_at",
        "error",
    ]
    return {
        "items": [{key: getattr(row, key) for key in keys} for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
