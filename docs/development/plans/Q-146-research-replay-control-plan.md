# Q-146 — Market replay control page implementation plan

> **For implementation agents:** read the spec and each affected repository's instructions. After Q-145 is Done
> and the plan is approved, launch `./work start Q-146 --agent <agent> --worktree`, then create the same branch
> from `development` in q_contracts, q_frontend and q under `.worktrees/`.

**Goal:** prepare, start, drive and shut down replay runs from the Research UI.
**Architecture:** a run manager inside the Research API drives `./dev replay` and forwards to the simulator's
admin listener; a new `/replay` workspace consumes contracted routes.
**Spec:** [Q-146 specification](../specs/Q-146-research-replay-control-spec.md).
**Status:** awaiting human review.

## Global constraints

- Deployments stay in q_terminal. No call to the replay control API on 8002.
- New contracts originate in q_contracts; vendor with `make contracts CONTRACTS_REPO=<worktree>`. Never hand-edit
  vendored code.
- Backend environment on `/mnt/build/venvs`. Run only the focused checks below; full CI runs before push.

## Ordered implementation

### 1. Contracts (q_contracts)

**Files:** `schema/api/replay/{overview,dataset,start-run}.schema.json`, `schema/api/openapi.yaml`,
`tests/test_replay_contracts.py`, `COMPAT.md`, generated output.

- [ ] Write fixtures for each phase, dataset state and start request; add the routes from the spec.
- [ ] Run `uv run python tools/validate.py` and `uv run pytest tests/test_replay_contracts.py`; regenerate; commit.
- [ ] Pin and vendor in q_backend and q_frontend; run `make contracts-check` in each.

### 2. Launcher (q)

**Files:** `tools/dev/replay.sh`, `tools/dev/research.sh`, `tools/tests/test-replay`, `README.md`.

- [ ] Write failing mocks for `--no-terminal`, `replay terminal`, `current.json` contents and mode 0600, per-run
  token generation, and `Q_REPLAY_CONTROL_ENABLED` and `Q_REPLAY_LAUNCHER` being set only in host mode.
- [ ] Implement them; run `bash -n` and `bash tools/tests/test-replay`, plus `tools/tests/test-research`; commit.

### 3. Run manager and routes (q_backend)

**Files:** new `src/q_backend/api/services/replay.py`, `src/q_backend/api/routers/replay.py`,
`src/q_backend/api/schemas/replay.py`, `tests/api/test_replay_control.py`; modify `src/q_backend/api/main.py`,
`src/q_backend/replay/{admin,controller,journal}.py` and `src/q_backend/cli/q_replay.py` (journal paging, and a
`freeze` command if Q-145 has not added one), `docs/operations/market-replay.md`, and the Q-145 spec note.

- [ ] Write failing tests with a fake launcher and a real admin listener: router absent without the setting;
  each route; start refused unless idle; reattach from `current.json`; forwarded stale revision; token absent
  from every response; freeze success, failure and no TickStore change; argument-list launcher calls.
- [ ] Implement; launcher calls and freezes run off the event loop and report through `overview` and dataset state.
- [ ] Run `pytest tests/api/test_replay_control.py tests/replay/test_admin.py -q` and changed-file Black and Ruff;
  commit.

### 4. Replay page (q_frontend)

**Files:** new `src/workspaces/replay/*`, `src/api/queries/replay.ts`, `src/mocks/replay.ts`, page tests; modify
`src/app/router.tsx`, `src/app/lazyWorkspaces.tsx`, `src/app/workspaceLoaders.ts`,
`src/components/dock/AppDock.tsx`, `src/mocks/handlers.ts`, and store workspace ids as needed.

- [ ] Write failing tests: setup validation; 404 unavailable notice; transport enablement per state; step only
  while paused; fresh `command_id` and last revision on each command; stale refusal refreshes without retry;
  error display; incremental journal; market time in both zones.
- [ ] Implement per the spec and `DESIGN.md`, with polling at 1 s active and 5 s idle.
- [ ] Run the page's tests with `TZ=America/Sao_Paulo pnpm vitest run`, `pnpm typecheck` and `pnpm lint`; inspect
  the page in browser mode with MSW; commit.

### 5. Handoff

- [ ] Update the operations document and README for the page and its host-mode requirement.
- [ ] Record one human session through acceptance 1, or the concrete blocker.
- [ ] `./work board set Q-146 in-review -m "<per-repository changes; checks and results; human session; follow-ups>"`.
