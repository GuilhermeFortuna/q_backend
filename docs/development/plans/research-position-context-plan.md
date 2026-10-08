# Research position context implementation plan

Spec: ../specs/research-position-context-spec.md

- [x] Create isolated `feat/research-position-context` branches from development in q_core and q_backend.
- [x] Extend the shared Rust candle loop with a fallible callback and per-bar signals; preserve static parity and protective fill ordering.
- [x] Project callbacks through PyO3 with owned buffers, strict return validation, and preserved exceptions.
- [x] Export ResearchPosition and adapt signatures once; integrate runtime hooks and explicit prepared-frame reuse.
- [x] Document both signatures and a direction-dependent exit example.
- [x] Verify Rust candle tests, binding wheel tests, research tests, focused backend engine tests, formatting and lint.
- [x] Review and commit focused changes on both branches.

Acceptance: actual filled positions only; next-open close/reverse; identical snapshots
for both hooks; flat context after executed stops/closures; isolated history and
immutable snapshots; legacy and mixed signatures; exceptions and invalid decisions;
static/callback ledger and trace parity; repeat runs; empty and gated/final bars.

Initial implementation delivery used a local wheel without committing dependency
paths. Release adoption was subsequently authorized and is recorded below.

## Validation and review

- Backend: 229 passed across `tests/research` and focused engine, bridge, signal,
  and registry baseline tests, against the built wheel; two existing Pydantic
  deprecation warnings. Existing strategy/backtest tests also passed (69) against
  the previously installed core.
- Rust: `cargo test -p q-engine`: 119 passed, including 5 callback tests and existing
  candle/decision/exit/protective/tick parity gates.
- Binding: `make wheel`, then `python tests/test_engine.py` passed with the wheel
  installed in a temporary environment. Backend validation loaded the same wheel
  from an extracted temporary directory without altering the shared environment.
- `cargo fmt --all --check`, focused Clippy, Ruff, and `git diff --check` passed.
- Independent review found timestamp backing storage shared across hook prefixes.
  Fixed in both runtime and legacy paths; regression tests failed before the fix
  and pass afterward, including attempts to modify the backing timestamps.

Deferred minor review suggestions: broaden input-buffer mutation tests beyond
prices; isolate nested mutable objects in object-valued indicator columns (pandas
copies these object references); include bar context in binding return-type errors.

No implementation rulings changed the approved behavior.

## Release adoption — 2026-10-08

- Published `q_core` tag `v2026.10.08.2` at
  `7e189227a51e7b3df50762148df79abdda0bd21e` after `make check` and the normal
  pre-push CI passed. The same-day package version remains `2026.10.8`.
- Updated `pyproject.toml` and `uv.lock` to this published tag and exact commit;
  `uv sync` installed it in the backend environment.
- Confirmed the installed candle API exposes `strategy_callback` and the public
  `ResearchPosition` export; all 229 focused research/engine tests passed against
  the published dependency, with the two existing Pydantic warnings.
- Backend publication runs its full CI hook. The resulting consumer commit and
  cross-repository verification evidence belong in `q_contracts/COMPAT.md`.
