# Q-109: Publish figures from research scripts

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 22 — Research figures
**Depends on:** none
**Repositories:** `q_backend` (owner), with the figure endpoints in `q_contracts`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-109-publish-figures-from-research-scripts-plan.md)

## Purpose

A research script can publish a finished backtest (`result.publish()`, Q-100), but any other visualization it draws ends in a `plt.show()` window or a file on disk. After this task one call sends a Matplotlib or Plotly figure to the Research stack, which stores it and serves it to the Figures tab that Q-110 adds to the Research workspace.

## Library (`q_backend.research`)

```python
from q_backend import research

research.use_figure_style()                      # optional: Q dark theme for both libraries
research.publish_figure(fig, title="Equity by regime", notes="2021-2025, H1")
```

`publish_figure(fig, *, title=None, collection=None, notes=None, replace=True, image_format="png", dpi=200, api_url=None, client=None) -> str` returns the figure id.

- **Accepted figures.** A `matplotlib.figure.Figure`, any object whose `.figure` is one (an `Axes`, a seaborn grid), or a `plotly.graph_objects.Figure`. The type is recognised without importing either library; anything else raises `TypeError` naming the accepted types.
- **Matplotlib** is rendered in the script with `savefig` at `dpi`, tight bounding box and the figure's own face colour, as `image_format` `"png"` (default) or `"svg"`. The figure is neither closed nor modified, and no display is needed.
- **Plotly** is sent as the figure's own JSON (`fig.to_json()`), so it stays interactive in the desktop.
- **`title`** defaults to the figure's own title (Matplotlib suptitle, else the title of a single axes; Plotly `layout.title.text`). With neither, `ValueError` names `title=`.
- **`collection`** groups figures in the desktop. It defaults to the running script's file stem, and to `"interactive"` when there is no script file.
- **`replace`**: with `True`, publishing a title that already exists in the collection updates that figure in place, so re-running a script does not pile up copies. `False` always adds a figure.
- **Provenance** (script path, git revision, dirty flag) is collected by the code `publish()` already uses and can never fail a publish.
- **Errors** follow `publish()`: an unreachable API raises `ConnectionError` naming `./dev research`; `422` raises `ValueError` with the server's detail; `413` raises `ValueError` stating the size and the limit; other failures raise `RuntimeError`. No retry.
- The base URL follows `Q_API_URL` / `api_url=` exactly as `publish()` does.

`use_figure_style()` applies the Q dark theme to the libraries that are installed: Matplotlib `rcParams` and a Plotly template named `q_dark` set as the default. Colours are copied from the `q_frontend` tokens (`src/styles/globals.css`, `src/lib/charts/chartTheme.ts`) into one module. It is opt-in; `publish_figure` never restyles a figure.

`matplotlib` and `plotly` become project dependencies. Importing `q_backend.research` still imports neither, nor httpx, storage, API or worker modules.

## Control API

Defined in `q_contracts` `schema/api/openapi.yaml`, generated and vendored; never hand-written in the consumer.

| Route | Behaviour |
| --- | --- |
| `POST /api/v1/research/figures` | Store a figure. `201` with `figure_id` and `replaced`. |
| `GET /api/v1/research/figures` | Metadata only, newest `updated_at` first. Query: `collection`, `limit` (default 100, maximum 500), `offset`. Returns `items`, `total`, `limit`, `offset` and `collections` (`name`, `count`, `updated_at` for every collection, unfiltered). |
| `GET /api/v1/research/figures/{figure_id}` | One figure's metadata. |
| `GET /api/v1/research/figures/{figure_id}/content` | The stored bytes with their media type, an `ETag` derived from the revision, `X-Content-Type-Options: nosniff` and a sandboxing `Content-Security-Policy`. |
| `DELETE /api/v1/research/figures/{figure_id}` | `204`; removes the row and the stored content. |
| `POST /api/v1/research/figures/bulk-delete` | Exactly one of `figure_ids` or `collection`; returns the existing bulk-delete response. |

An unknown id is `404` on every route.

**Request.** `title` (1–200 characters), `collection` (1–120), optional `notes` (up to 4000), `replace` (default `true`), `provenance` (`script`, `git_revision`, `git_dirty`, all optional) and `content`, discriminated by `kind`:

- `image`: `media_type` (`image/png` or `image/svg+xml`), `data` (base64), `width` and `height` in pixels;
- `plotly`: `figure`, an object with a `data` array and an optional `layout` object.

**Metadata.** `figure_id`, `title`, `collection`, `notes`, `kind`, `media_type` (`application/json` for Plotly), `width`, `height` (null for Plotly), `byte_size`, `revision`, `created_at`, `updated_at`, `provenance`. A replacement keeps `figure_id` and `created_at`, increments `revision` and sets `updated_at`.

**Validation.** `422` for blank title or collection, undecodable base64, PNG bytes without the PNG signature, SVG that is not well-formed XML with an `svg` root, non-positive dimensions, or a Plotly figure without a `data` array. `413` when the decoded content exceeds `Q_RESEARCH_FIGURE_MAX_BYTES` (default 32 MiB). The stack stores figures; it never executes or renders them.

## Storage

- Table `research_figures` (one Alembic migration) holds the metadata, indexed on `(collection, title)` and `updated_at`.
- Content lives in the lake at `research_figures/<figure_id>/content.<png|svg|json>`, written to a temporary file and renamed, so a reader never sees a partial figure. A replacement that changes the extension removes the old file.
- A failed write leaves neither a row nor a file; a delete removes both.

## Documentation

`docs/research-library.md` gains **Publish figures to the Research stack** after the backtest publishing section (both libraries, collections, replacement, the style helper, the size limit), and the README's API reference lists the routes. One example script under `examples/research/` publishes a Matplotlib and a Plotly figure built from synthetic data.

## Focused acceptance

Tests use the API test client with a temporary lake and database, mocked HTTP for the library, and the Matplotlib `Agg` backend. No running stack, gateway, Docker, GPU or desktop.

1. A Matplotlib figure publishes as PNG and as SVG with the pixel size of the rendered image; the figure object is unchanged and still open.
2. An `Axes` publishes its parent figure; a Plotly figure publishes its `to_json()` content unchanged, including typed-array traces.
3. Title, collection and provenance defaults resolve as specified; a missing title and an unsupported object raise the documented errors.
4. Each documented transport and status error raises the documented exception.
5. `POST` then `GET .../content` returns byte-identical content with the stored media type and the security headers.
6. Publishing the same collection and title replaces in place (`replaced: true`, same id, `revision` + 1, new content); `replace: false` adds a second figure.
7. The list is ordered by `updated_at`, filters by collection, pages, and reports every collection with its count.
8. Each validation rule returns `422`, the size limit returns `413`, and neither leaves a row or a file.
9. Delete and both bulk-delete forms remove rows and files; unknown ids return `404`.
10. `use_figure_style()` sets the Matplotlib and Plotly defaults and works with only one of the libraries importable.
11. A fresh `import q_backend.research` loads neither `matplotlib`, `plotly` nor `httpx`.
12. `make check` passes in `q_contracts`; `make contracts-check` passes in `q_backend` against the task's contract commit; the migration upgrades and downgrades.

## Delivery boundary

- No desktop work (Q-110), no stream topic (the desktop polls the list), no server-side rendering or thumbnails, no editing of a stored figure other than replacement.
- Tables, text reports and arbitrary HTML are not figures and are not accepted.
- `BacktestResult.publish()` and the backtest import route are unchanged.
