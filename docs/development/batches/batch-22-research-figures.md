# Batch 22 — Research figures

**Status:** issues published on 2026-10-12; written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with the figure endpoints in q_contracts and a desktop task in q_frontend.

## Outcome

A research script sends a Matplotlib or Plotly figure to the Research stack with `research.publish_figure(fig)`. It appears within seconds in a **Figures** tab, the first tab of the Research workspace and the one it opens on.

## Findings this batch answers

Each was read from the code on 2026-10-12.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A script can publish a backtest and nothing else. | `q_backend.research` exports `BacktestResult.publish()` (Q-100); `research/publishing.py` posts only to `POST /api/v1/backtests/import`. | Q-109 |
| 2 | The stack has nowhere to keep a figure. | No table, lake path or route stores an image or a chart specification; the lake holds parquet and JSON results keyed by run. | Q-109 |
| 3 | Neither plotting library is installed. | `pyproject.toml` lists neither `matplotlib` nor `plotly`; the one plotting script (`scripts/ml-experiment/test_kronos.py` on `research/ml-experiment`) imports `matplotlib.pyplot`. | Q-109 |
| 4 | The Research workspace opens on the Feature Store. | `parseResearchTab` in `q_frontend/src/app/router.tsx` and `ResearchWorkspace` both default to `store`; `TAB_OPTIONS` has six tabs and none shows script output. | Q-110 |
| 5 | The desktop has no renderer for arbitrary figures. | Charts are purpose-built with recharts, visx, d3 and three; there is no Plotly dependency and no image gallery. | Q-110 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| [Q-109](https://github.com/GuilhermeFortuna/q_backend/issues/43) — Publish figures from research scripts | q_backend (with q_contracts) | none | [Spec](../specs/Q-109-publish-figures-from-research-scripts-spec.md) | [Plan](../plans/Q-109-publish-figures-from-research-scripts-plan.md) |
| [Q-110](https://github.com/GuilhermeFortuna/q_frontend/issues/9) — Figures tab in the Research workspace | q_frontend | Q-109 | `q_frontend/docs/development/specs/Q-110-figures-tab-in-the-research-workspace-spec.md` | `q_frontend/docs/development/plans/Q-110-figures-tab-in-the-research-workspace-plan.md` |

Implement Q-109, then Q-110. The work is two tasks because each is a full agent pass with its own review surface: Q-109 is a contract, a migration, storage, six routes and a library, verified without a desktop; Q-110 is a visual workspace verified against MSW. The contract is the only thing they share.

## Shared decisions

- **Both libraries, each in its best form.** Matplotlib figures are rendered in the script and stored as PNG (default) or SVG. Plotly figures are stored as their JSON and stay interactive in the desktop. One call, `publish_figure(fig)`, accepts either.
- **Publish results; the stack never runs script code.** The stack stores bytes and JSON and serves them back. It neither renders nor evaluates a figure, and the desktop shows SVG only through `<img>`.
- **Collections group, titles identify.** A figure belongs to a collection (the script's name by default). Publishing the same title into the same collection replaces the figure in place, so re-running a script refreshes the tab instead of filling it. `replace=False` keeps both.
- **Over the control API.** The library posts to the API, as `publish()` does; only publishing needs the stack running.
- **Polling, not a stream topic.** The tab polls the metadata list every 2 seconds while visible. A new stream topic would add a contract, retention policy and reconciler path for a list that is cheap to read locally.
- **Figures look native when asked to.** `use_figure_style()` gives both libraries the Q dark palette; it is opt-in and publishing never restyles a figure.
- **Provenance travels with the figure.** Script path and git revision are stored so a reviewer knows what drew it.
- **Focused verification.** API test client, mocked HTTP, the `Agg` backend and MSW. No gateway, worker, Docker, GPU or desktop run for automated checks. One manual step closes the batch: run the example script against `./dev research` with the Figures tab open.

## Recorded, not in this batch

- **Tables, metrics and text.** A DataFrame or a markdown note beside a figure would turn collections into reports. Not accepted by Q-109; a follow-up once figures are in use.
- **Figures attached to a published backtest.** A figure is not linked to a `backtest_runs` row.
- **Static thumbnails for Plotly.** Cards draw a non-interactive plot instead; server-side rendering would need a headless browser in the stack.
- **Retention.** Figures stay until deleted from the desktop or the API.

## Review and integration

Documentation is committed on local `docs/batch-22-research-figures` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-22-research-figures`. No branch is pushed or merged by the authoring session. The complete specification and plan text is embedded in each issue so remote review is possible before publication.

The human integrates these documentation branches before launching implementation tasks, and owns the initial board Status and the move to Todo. The authoring session adds the issues and their Batch and Depends on metadata only. No product code is changed by the authoring session.

Launch an approved task from the workspace root with `./work start Q-NNN --agent <agent> --worktree`. Finish Q-109 with `./work finish Q-109 --push`: Q-110 needs the Q-109 contract commit reachable on the `q_contracts` remote, because `make contracts` fetches the pinned revision from there. Q-109 adds a database migration that the operator applies to the Research database after merge.
