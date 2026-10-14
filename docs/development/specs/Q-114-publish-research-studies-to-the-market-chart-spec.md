# Q-114: Publish research studies to the Market chart

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 25 — Market chart studies
**Depends on:** none
**Repositories:** `q_backend` (owner), with the routes in `q_contracts` and the chart in `q_frontend`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-114-publish-research-studies-to-the-market-chart-plan.md)

## Purpose

Research scripts mostly compute series meant to be read over price: a custom band, a regime score, a filter's state. Today such a series reaches the desktop only as a Matplotlib or Plotly figure (Q-109), detached from the Market chart where the bars, the catalog indicators and the drawing tools are. After this task one call publishes a pandas Series or DataFrame indexed by bar time as a **study** for one symbol and timeframe. The Market chart lists it in its indicator menu while that symbol is on the chart and draws it like a catalog indicator.

## Current system

- The Market chart draws catalog indicators computed by the backend (Q-112). Their values come from `POST /api/v1/market/indicators/{symbol}/series` aligned to the chart's bar times, in a price pane or an oscillator pane.
- `publish_figure` (Q-109) stores figures in table `research_figures` with content in the lake, and collects provenance with `collect_figure_provenance`.
- `load_bars` returns frames indexed by timezone-aware Brasília bar open times. The API serializes bar times as UTC ISO-8601.
- Chart profiles (`quant:chart-profiles` in `localStorage`) are global, not per symbol.

## Library (`q_backend.research`)

```python
from q_backend import research
from q_backend.research import ChartIndicator

bars = research.load_bars("WDO$N", timeframe="M10", start="2026-01-01")
study = keltner(bars)                                  # DataFrame on bars.index

research.publish_study(study, symbol="WDO$N", name="Keltner 20/2")
research.publish_study(
    study,
    symbol="WDO$N",
    name="Keltner 20/2 + width",
    series=[
        ChartIndicator("kc_upper", color="#6eb5ff"),
        ChartIndicator("kc_lower", color="#6eb5ff"),
        ChartIndicator("kc_width", pane="oscillator", label="Width"),
    ],
)
```

`publish_study(data, *, symbol, name, timeframe=None, series=None, pane="price", notes=None, replace=True, api_url=None, client=None) -> str` returns the study id.

- **Data.** A `pandas.Series` or `DataFrame` with a `DatetimeIndex`. The index must be strictly increasing. A timezone-aware index is converted to UTC. A naive index is read as Brasília exchange time, as `load_bars` reads naive input. Every published column must be numeric or boolean; booleans become `0` and `1`. `NaN` and infinities become missing values. An unnamed Series is published under the key `value`.
- **Series.** Without `series`, every column is published in `pane` with its name as key and label. With `series`, a list of `ChartIndicator`, only the named columns are published, each with its own pane, label, colour, line style and width. A missing column, a duplicate column or an empty selection raises `ValueError`.
- **Timeframe.** `timeframe` is one of the chart timeframes (`M1`, `M5`, `M10`, `M15`, `M30`, `H1`, `H2`, `H4`, `D1`). When omitted, it is the smallest gap between consecutive timestamps, which must equal one of them. With either, every gap must be a whole multiple of the timeframe and every timestamp must have zero seconds. Otherwise `ValueError` names the gap found and `timeframe=`.
- **Symbol** is upper-cased and must be non-empty. A study is shown for that exact symbol only: one published for `WDO$N` does not appear on `WDOV26`.
- **Identity.** A study is identified by symbol, timeframe and name. With `replace=True`, publishing an existing identity replaces its values and styling in place and keeps its id. With `replace=False` the API answers `409` and the library raises `ValueError`.
- **Provenance**, **base URL** (`Q_API_URL` / `api_url=`) and **errors** follow `publish_figure`: `ConnectionError` naming `./dev research`, `ValueError` for `409`, `413` and `422` with the server's detail, `RuntimeError` otherwise. No retry.
- Importing `q_backend.research` still imports neither httpx nor storage, API or worker modules.

## Control API

Defined in `q_contracts` `schema/api/openapi.yaml`, generated and vendored; never hand-written in the consumer.

| Route | Behaviour |
| --- | --- |
| `POST /api/v1/research/studies` | Store a study. `201` with `studyId` and `replaced`. |
| `GET /api/v1/research/studies` | Metadata only, for `symbol` (required) and optional `timeframe`, newest `updatedAt` first. |
| `GET /api/v1/research/studies/{study_id}/series` | Values between `start` and `end` (inclusive, ISO-8601): `timestamps` (UTC ISO-8601) and, per series, `key` and `values` with `null` for missing values. |
| `PATCH /api/v1/research/studies/{study_id}` | Rename (`name`). `409` when the new identity exists. |
| `DELETE /api/v1/research/studies/{study_id}` | `204`; removes the row and the stored values. |

An unknown id is `404` on every route.

**Request.** `name` (1–120 characters), `symbol`, `timeframe`, optional `notes` (up to 4000), `replace` (default `true`), `provenance` (as figures), `timestamps` (UTC ISO-8601, strictly increasing) and `series` (1–32 items): `key`, `label`, `pane` (`price` or `oscillator`), optional `color`, `lineStyle` (`solid`, `dashed`, `dotted`), `lineWidth` (positive), and `values` (numbers or `null`, one per timestamp).

**Metadata.** `studyId`, `name`, `symbol`, `timeframe`, `notes`, the series without their values, `start`, `end`, `pointCount`, `revision`, `createdAt`, `updatedAt`, `provenance`. A replacement keeps `studyId` and `createdAt`, increments `revision` and sets `updatedAt`.

**Validation.** `422` for a blank name or symbol, an unknown timeframe, timestamps that are unordered or repeated, a value array whose length differs from `timestamps`, duplicate series keys, an unknown pane or line style, a non-positive line width, or a series whose values are all `null`. `413` when timestamps × series exceeds `Q_RESEARCH_STUDY_MAX_VALUES` (default 2,000,000).

## Storage

- Table `research_studies` (one Alembic migration) holds the metadata and the series styling, with a unique constraint on `(symbol, timeframe, name)` and an index on `(symbol, updated_at)`.
- Values live in the lake at `research_studies/<study_id>/values.parquet` (a `time` column and one column per series key), written to a temporary file and renamed, as figures are. A failed write leaves neither a row nor a file; a delete removes both.
- The series route reads only the requested time range.

## Market chart

- **Menu.** The indicator menu gains a **Studies** group listing the studies of the symbol on the chart, each with its name, timeframe, update time and notes. Studies of another symbol are not listed. A study on another timeframe is listed but disabled, with "Published at M10". The list is refetched when the menu opens and when the window regains focus, so a study published from a terminal appears without reloading.
- **Selection.** Enabled studies are stored per symbol in `localStorage` (`quant:market-studies`), apart from chart profiles. A stored id that no longer exists is dropped. Rename and delete are offered from the menu; delete asks for confirmation in the app, never through a browser dialog.
- **Drawing.** The chart fetches each enabled study's values for the range of its loaded bars, keyed by study id and `revision`, and refetches when the loaded bars or the revision change. Values are placed on the bar with the same timestamp; a bar with no value breaks the line. Price-pane series overlay the candles; each study's oscillator series share one oscillator pane labelled with the study name. Unstyled series take colours from the chart theme. The legend labels a series `<study name> · <series label>`.
- **Misalignment.** When values in the loaded range fall on no bar, the chart notice reads "<study>: N of M points fall between bars; check the timezone or whether timestamps are bar open times." Nothing is shifted or resampled.

## Documentation

`docs/research-library.md` gains **Publish studies to the Market chart** after the figures section: data requirements, timeframe inference, styling with `ChartIndicator`, replacement, symbol scoping and the size limit. The README's API reference lists the routes. `examples/research/publish_study.py` loads bars, computes a band and a width with `research.indicators`, and publishes them.

## Focused acceptance

Tests use the API test client with a temporary lake and database, mocked HTTP for the library, and component tests against mocked endpoints. No running stack, gateway, Docker, GPU or desktop.

1. A Series and a DataFrame on an aware index publish UTC timestamps; a naive index is read as Brasília time; `NaN` and infinities become `null` and booleans `0` and `1`.
2. Timeframe inference returns `M10` for M10 bars with overnight gaps; an irregular index, seconds in a timestamp, or an explicit `timeframe` that does not divide a gap raises the documented `ValueError`.
3. `series=` publishes only the named columns with their panes and styling; a missing or duplicate column raises `ValueError`.
4. Republishing an identity keeps `studyId` and `createdAt` and increments `revision`; `replace=False` raises `ValueError` on `409`.
5. Each documented validation failure returns `422`; exceeding the value limit returns `413`.
6. `POST` then `GET .../series` over a sub-range returns exactly the stored timestamps and values in that range.
7. Listing by symbol returns only that symbol's studies; rename onto an existing identity returns `409`; delete removes the row and the Parquet file.
8. The Studies group lists the chart symbol's studies, disables one on another timeframe with its timeframe, and lists none after the symbol changes.
9. An enabled study draws its price series on the candles and its oscillator series in one pane, with a gap where a bar has no value; a new `revision` refetches its values.
10. Values falling between bars produce the misalignment notice with the right counts.
11. Enabled studies persist per symbol across reloads, and a deleted study is dropped from storage.
12. `make contracts-check` passes in `q_backend` and `q_frontend`; the focused tests and each repository's lint and type checks pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-114 --agent <agent> --worktree`.
The task branch also exists in `q_contracts` and `q_frontend`. No change to the live terminal, `q_terminal`, the catalog indicators, chart profiles or the figures feature; no markers, shapes or text annotations; no resampling between timeframes; no live-updating studies.
