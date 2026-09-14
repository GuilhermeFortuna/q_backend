from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles


# Allow SQLite to compile postgresql.JSONB columns as JSON (used in migration tests on SQLite).
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@dataclass(frozen=True)
class SchemaRevision:
    current: str | None
    head: str


def alembic_ini_path() -> Path:
    """Return the absolute path to alembic.ini at the checkout root."""
    return Path(__file__).resolve().parents[4] / "alembic.ini"


def schema_revision(engine: Engine, alembic_ini: Path) -> SchemaRevision:
    """Inspect the database schema revision and the Alembic script head revision."""
    alembic_cfg = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(alembic_cfg)
    head = script.get_current_head()
    if head is None:
        heads = script.get_heads()
        head = heads[0] if heads else ""

    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current = context.get_current_revision()

    return SchemaRevision(current=current, head=head)
