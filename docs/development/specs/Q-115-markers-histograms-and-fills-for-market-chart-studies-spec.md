# Q-115: Markers, histograms and fills for Market chart studies

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 26 — Market chart study plot kinds
**Depends on:** none
**Repositories:** `q_backend` (owner), with the schema in `q_contracts` and the chart in `q_frontend`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-115-markers-histograms-and-fills-for-market-chart-studies-plan.md)

## Purpose

Q-114 lets a research script publish a study to the Market chart, but every series is drawn as a line. Much of what research produces is not a line: entry and exit signals, events, a regime state, a score read against zero, a band whose inside matters. After this task a study series can be drawn as markers, a stepped line, a histogram, or a line with a filled area to another series. A boolean signal column becomes arrows under or over the bars with one `ChartIndicator`.

The study size limit also changes from rows × series to non-null values, so sparse marker series cost what they draw.

## Current system

- `ResearchStudySeriesInput` carries `key`, `label`, `pane` (`price` or `oscillator`), `color`, `lineStyle`, `lineWidth` and dense `values` (number or `null` per timestamp). Styling is stored in the JSON `series` column of `research_studies`; values in the lake at `research_studies/<study_id>/values.parquet`.
- `create_or_replace_study` answers `413` when `len(timestamps) × len(series)` exceeds `research_study_max_values` (`Q_RESEARCH_STUDY_MAX_VALUES`, default 2,000,000).
- `q_backend.research.charting.ChartIndicator` styles study columns in `publish_study` and also declares a research strategy's `chart_indicators` for the backtest Trade Chart (`ChartIndicatorSeries`).
- `buildStudyRenderModel` turns price-pane series into `PriceLineRenderGroup`s drawn by `IndicatorLayer`, and each study's oscillator series into one `OscillatorPane` of lines.
- `LiveStrategyChart` draws buy, sell, close and fill markers with a private `markerGlyph` function.

## Series kinds

Each study series gains an optional `kind`; absent means `line`, so studies already stored draw as before and need no migration.

| `kind` | Panes | Draws | Fields |
| --- | --- | --- | --- |
| `line` | both | a line through non-null values, broken at `null` | `color`, `lineStyle`, `lineWidth`, `fillTo`, `fillOpacity` |
| `step` | both | a horizontal step at each value, held until the next bar, broken at `null` | as `line` |
| `markers` | both | one glyph per drawn point | `color`, `shape`, `size`, `position` |
| `histogram` | oscillator | one column per non-null value, from `baseline` to the value | `color`, `negativeColor`, `baseline` |

- **Markers.** `shape` is `circle` (default), `square`, `diamond`, `triangle-up`, `triangle-down` or `cross`; `size` is the glyph width in pixels, greater than 0 and at most 32 (default 8). `position` is `value` (default), `above-bar` or `below-bar`. With `value` every non-null value draws a glyph at that value. With `above-bar` or `below-bar`, a glyph is drawn above the bar's high or below its low for every non-null, non-zero value, so a boolean column (published as `0` and `1`) marks only the bars where it is true. `above-bar` and `below-bar` are allowed only in the `price` pane.
- **Histogram.** Columns at or above `baseline` (default `0`) use `color`; columns below it use `negativeColor` when set, otherwise `color`.
- **Fill.** A `line` or `step` series with `fillTo` names another series of the same study and pane, of kind `line` or `step`. The area between the two is shaded in the series' colour at `fillOpacity` (greater than 0 and at most 1, default `0.15`) wherever both values are non-null. A series cannot fill to itself.
- A field that does not apply to the series' kind is rejected, so a misspelt intent fails at publish time instead of being drawn wrongly.

## Library (`q_backend.research`)

```python
research.publish_study(
    frame,
    symbol="WDO$N",
    name="Breakout v3",
    series=[
        ChartIndicator("upper", color="#6eb5ff"),
        ChartIndicator("lower", color="#6eb5ff", fill_to="upper"),
        ChartIndicator("long_entry", kind="markers", shape="triangle-up", position="below-bar", color="#26a69a"),
        ChartIndicator("short_entry", kind="markers", shape="triangle-down", position="above-bar", color="#ef5350"),
        ChartIndicator("regime", kind="step", pane="oscillator"),
        ChartIndicator("score", kind="histogram", pane="oscillator", negative_color="#ef5350"),
    ],
)
```

- `ChartIndicator` gains `kind`, `shape`, `size`, `position`, `negative_color`, `baseline`, `fill_to` and `fill_opacity`, validated in `__post_init__` with the rules above. `fill_to` is checked against the other selected columns in `publish_study`; an unknown or invalid target raises `ValueError` naming it.
- `publish_study` without `series` still publishes every column as a `line`.
- A research strategy whose `chart_indicators` declares a kind other than `line`, or any of the new fields, raises `ValueError` saying they apply to studies only. The backtest Trade Chart and `ChartIndicatorSeries` do not change.

## Control API

Defined in `q_contracts` `schema/api/openapi.yaml`, generated and vendored.

- `ResearchStudySeriesInput` and `ResearchStudySeriesMetadata` gain optional `kind`, `shape`, `size`, `position`, `negativeColor`, `baseline`, `fillTo` and `fillOpacity`, with the enums and bounds above. Metadata of a study stored before this task has no `kind` and is read as `line`.
- **Validation** adds `422` for an unknown kind, shape or position; a field not allowed for the kind; `histogram`, or markers at `above-bar`/`below-bar`, outside their allowed pane; a `size` or `fillOpacity` out of range; a `fillTo` that names the series itself, a missing key, a series in another pane or one that is not `line` or `step`.
- **Size limit.** `413` when the non-null values across all series exceed `Q_RESEARCH_STUDY_MAX_VALUES` (default 2,000,000), with detail "Study has N non-null values; the limit is L". Because a mostly-null study can still be a large request, `413` also applies when `timestamps` exceeds `Q_RESEARCH_STUDY_MAX_TIMESTAMPS` (default 2,000,000, about fourteen years of WDO M1 bars). `docs/research-library.md` states both limits.

## Market chart

- **Render model.** Price-pane study series become typed layers (`line`, `step`, `markers`), with an optional fill on lines and steps; oscillator outputs gain the same kinds plus `histogram`. Catalog indicators keep producing lines; the types allow them to use the new kinds in a later task without another redesign.
- **Glyphs.** Marker shapes live in one module under `components/charts/layers/`. `LiveStrategyChart` draws its buy, sell, close and fill markers through it and looks as before.
- **Drawing.** Markers at `above-bar`/`below-bar` sit a fixed pixel gap from the bar's high or low, so they keep their distance while zooming. Only markers inside the visible bar range are rendered. Each glyph carries an SVG `<title>` of `<study name> · <label>: <value>`. Fills are drawn beneath the candles and every line, so they hide neither. The oscillator pane's y range includes the histogram baseline.
- **Legend.** A series' swatch shows its kind: a line, a step, its marker glyph or a column.
- Selection, storage, refetch and the misalignment notice behave as in Q-114. Values are still never moved to a neighbouring bar.

## Documentation

`docs/research-library.md` extends **Publish studies to the Market chart** with the kinds, the boolean-signal pattern, fills, the field rules and the new limits. `examples/research/publish_study.py` adds entry markers and a histogram to its band.

## Focused acceptance

Tests use the API test client with a temporary lake and database, mocked HTTP for the library, and component tests against mocked endpoints. No running stack, gateway, Docker, GPU or desktop.

1. A series without `kind` publishes, stores and reads back as `line`; a study stored by Q-114 still draws as lines.
2. `ChartIndicator` accepts each kind with its fields and raises `ValueError` for each rule in **Series kinds**; `publish_study` raises for an invalid `fill_to`.
3. A strategy declaring a non-line `ChartIndicator` raises the studies-only `ValueError`.
4. Each new validation rule returns `422`.
5. A study of 32 series × 1,000,000 timestamps with 500,000 non-null values in total is accepted; 2,000,001 non-null values or 2,000,001 timestamps return `413` with the documented detail.
6. Markers at `value` draw at their values; at `below-bar`/`above-bar` they draw only for non-zero values, below the low or above the high, and none are rendered outside the visible range.
7. A histogram draws columns from its baseline with `negativeColor` below it; a step holds its value until the next bar and breaks at `null`.
8. A fill shades only where both series are non-null.
9. `LiveStrategyChart` marker tests pass unchanged.
10. `make contracts-check` passes in `q_backend` and `q_frontend`; the focused tests and each repository's lint and type checks pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-115 --agent <agent> --worktree`.
The task branch also exists in `q_contracts` and `q_frontend`. No change to the backtest Trade Chart, `ChartIndicatorSeries`, catalog indicators, `q_terminal` or the figures feature; no per-point colours or text labels; no shapes spanning time ranges; no resampling between timeframes.
