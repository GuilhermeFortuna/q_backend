# Q-109 implementation plan: Publish figures from research scripts

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-109 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** `research.publish_figure(fig)` stores a Matplotlib or Plotly figure in the Research stack, and the control API lists, serves and deletes stored figures.
**Architecture:** The script renders or serializes the figure and posts it to the control API; the API keeps metadata in PostgreSQL and content in the lake. The wire shape is defined in `q_contracts`.
**Tech stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, httpx, pydantic contract models, Matplotlib, Plotly, pytest.
**Spec:** [Specification](../specs/Q-109-publish-figures-from-research-scripts-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Define the routes and schemas in `q_contracts` first; build requests and responses from the vendored models. Never hand-edit vendored contract code.
- The `q_contracts` change is committed on a `Q-109-publish-figures-from-research-scripts` branch created in that repository; `q_backend` vendors it with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
- Import Matplotlib, Plotly and httpx inside the functions that need them.
- The API stores bytes and JSON as received. It never imports Matplotlib or Plotly and never evaluates figure content.
- Move the provenance helpers out of `research/publishing.py` into a shared module; `publish()` keeps its behaviour and its tests.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- A failed or oversized publish leaves no row and no file; a replacement is never observable half-written.
- The content route cannot serve a path outside the figure's lake directory, and an SVG opened directly cannot run script.
- `publish_figure` does not close, resize or restyle the caller's figure, and works without a display.
- Plotly content round-trips byte-for-byte; no float cleaning rewrites it.
- Error messages tell the user what to do next.

## Ordered implementation

### 1. Contract

**Files (`q_contracts`):** `schema/api/openapi.yaml`, generated outputs, a `tests/test_research_figures_contract.py`.

- [x] Add a failing contract test: the six routes, the `content` discriminator, the metadata shape and the bulk-delete one-of.
- [x] Add the paths and schemas, regenerate, run `make check`, commit.
- [x] In `q_backend`: update `CONTRACTS_REV`, run `make contracts` against the local clone and `make contracts-check`; commit.

### 2. Storage

**Files:** `src/q_backend/storage/db/models.py`, `storage/db/repositories.py`, `storage/lake/artifacts.py`, `storage/settings.py`, a new `alembic/versions/*_research_figures.py`, `tests/storage/`.

- [x] Add failing tests for create, replace, list with collection counts, delete, atomic content write and extension change on replacement.
- [x] Implement the model, migration, repository functions, lake read/write/delete and the `research_figure_max_bytes` setting.
- [x] Run the storage tests and the migration upgrade/downgrade check; commit.

### 3. Control API

**Files:** new `src/q_backend/api/routers/research_figures.py` and `api/schemas/research_figures.py`, a service module beside `backtesting/run_import.py` conventions, `api/main.py`, `tests/api/`.

- [x] Add failing tests for spec acceptance items 5–9.
- [x] Implement validation, the create/replace transaction, the content response with its headers, and deletion.
- [x] Run the API tests; commit.

### 4. Library

**Files:** new `src/q_backend/research/figures.py`, `research/figure_style.py` and `research/provenance.py`; modify `research/publishing.py`, `research/__init__.py`, `pyproject.toml`, `uv.lock`; `tests/research/test_figures.py`, `tests/research/test_imports.py`.

- [ ] Add failing tests for spec acceptance items 1–4, 10 and 11.
- [ ] Add the dependencies; implement figure recognition, rendering, defaults, the request and error mapping; export `publish_figure` and `use_figure_style` lazily.
- [ ] Run `uv run pytest tests/research -q`; commit.

### 5. Documentation and example

**Files:** `docs/research-library.md`, `README.md`, `examples/research/publish_figures.py`, `tests/research/test_examples.py`.

- [ ] Write the guide section and the route reference; add the example with mocked HTTP coverage.
- [ ] Commit.

## Verification

- `uv run pytest tests/research tests/api tests/storage -q` for the changed areas; `ruff` and `black` on touched files; `make contracts-check`.
- `make check` in `q_contracts`.
- One manual step after merge: apply the migration to the Research database, run the example against `./dev research`, and read both figures back with `GET /api/v1/research/figures`.
- Review the whole change, then `./work board set Q-109 in-review` with results. Finish with `./work finish Q-109 --push` so the contract commit is published for Q-110.
