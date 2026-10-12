# Q-113: Quote features outside the genome

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 24 — Quote feature corrections
**Depends on:** Q-112
**Repositories:** `q_backend` only.
**Implementation plan:** [Plan](../plans/Q-113-quote-features-outside-the-genome-plan.md)

## Purpose

Q-112 added quoted spread and spread against volatility to the feature store by registering them as genome node kinds. The genome cannot evaluate them, the feature store never reads their ticks, and feature evaluation fails on them. The unit suite on `development` has 14 failures, so the branch cannot be pushed. After this task the two features are tick-store features that the genome does not know about, they return values where ticks exist, and the causality invariant covers them.

## Current system

Observed on `development` at `5a71e95`.

- **Registered as genome nodes.** `NODE_SPECS` contains `feature.quote_spread` and `feature.spread_vs_volatility`, although Q-112 excludes quote indicators as genome nodes. A genome that uses either passes validation, and `CompositeStrategy.compute_indicators` then raises `Unsupported feature node kind`. Both appear in the capability registry, in the strategy-builder system prompt (39,997 characters against a budget of 39,638) and in `score_bias.FEATURE_NAME_TO_KIND`.
- **Evaluation fails.** `_resolve_leakage_status` (`features/evaluation.py`) and the evidence matrix builder (`features/evidence_service.py`) call `compute_feature` without a symbol, which raises `ValueError` for a quote feature. The latent gate's classical baseline and the encoder ablation request every `classical` feature, so both fail on every run.
- **No values in the feature store.** `_compute_quote_feature` opens a tick store only when given an explicit root, and `build_feature_matrix` passes none, so every value is missing. The chart service opens the default store for the same request.
- **Failing tests** under `scripts/ci.sh`: `test_feature_spec_is_causal` for both features; `test_prefix_causality` and `test_node_kind_is_causal` for both kinds; `test_context_features_in_add_node_pool`; `test_system_prompt_length_budget`; four tests in `tests/neural/test_latent_gate.py`; two in `tests/api/test_encoder_ablation_jobs.py`.

## Required behavior

### Registry

- `NODE_SPECS` no longer contains `feature.quote_spread` or `feature.spread_vs_volatility`. A genome that names either is rejected at validation as an unknown node kind.
- The features `quote_spread` and `spread_vs_volatility` keep their name, version, category, parameter keys and defaults, so their feature identifiers do not change. Their parameter keys are declared in the feature registry.
- Both features have source `quote`. Their `node_kind` keeps its current string and is, for this source, a recipe identifier only: the stored feature version and the control API both require a value, and neither changes.
- `assert_catalog_consistent` requires a `quote` feature to name a recipe that is absent from `NODE_SPECS`, to keep its defaults inside its parameter keys and to have a forward window of zero. Every `classical` and `exogenous` feature must still reference a node kind in `NODE_SPECS`.
- `compute_feature` selects the tick-store path by source, not by comparing node-kind strings.

### Computation

- A quote feature opens the tick store for the request's symbol. With no explicit root this is the store named by `Q_RESEARCH_TICK_STORE`, else the default store. The chart service and the feature store resolve the store through one shared function.
- A store that cannot be opened, or that holds no ticks for the range, yields a feature whose values are all missing. It is not an error.
- The leakage check in feature evaluation and the evidence matrix builder pass the run's symbol to `compute_feature`.
- `compute_feature` without a symbol still raises `ValueError` for a quote feature.

### Latent gate

The latent gate's baseline stays the best `classical` feature. Quote features have their own source and are therefore not part of it, so a gate result does not depend on which days have cached ticks. A feature evaluation that names a quote feature explicitly scores it like any other feature.

### Causality coverage

- `test_feature_spec_is_causal` covers both quote features against a synthetic tick store under `tmp_path`. The quotes span the synthetic bars and their spread varies from bar to bar, so a shifted series cannot pass.
- A companion test plants a one-bar lookahead on the quote path and asserts that the harness reports it.
- `EXEMPT_SPECS` stays empty and `EXEMPT_NODE_KINDS` is unchanged.

## Focused acceptance

Synthetic bars and ticks, stores under `tmp_path`, no gateway, Docker, GPU or desktop beyond what `scripts/ci.sh` starts.

1. `NODE_SPECS`, `add_node_kinds()`, the capability registry, `FEATURE_NAME_TO_KIND` and the strategy-builder system prompt contain neither quote kind; a genome naming one fails validation.
2. `list_feature_specs()` returns both features with source `quote`; their identifiers for default parameters equal the identifiers at `5a71e95`.
3. With `Q_RESEARCH_TICK_STORE` pointing at synthetic ticks and no explicit root, `build_feature_matrix` returns the Q-111 aggregate for `quote_spread` and a finite `spread_vs_volatility`; over a range without ticks both columns are entirely missing.
4. A feature evaluation whose feature set includes a quote feature completes with and without ticks, and its leakage status comes from the causal check.
5. The classical feature set used by the latent gate contains no quote feature; the latent gate and encoder ablation tests pass without modification.
6. Both quote features pass `test_feature_spec_is_causal` through the tick-backed harness, and the planted lookahead is caught.
7. `test_system_prompt_length_budget` passes. Its baseline changes only if the prompt measured after the removal still exceeds the budget; it is then set to the measured length and the review note states both numbers.
8. The unit stage of `scripts/ci.sh` passes with no test deleted or skipped. The whole suite is required here because this task exists to restore it.

## Delivery boundary

- No quote indicator becomes a genome node; that still needs ticks inside the candle backtest frame.
- No change to `q_contracts`, `q_frontend`, the database schema, the chart indicator catalog or the series endpoint. Making `node_kind` optional would touch all three and is not needed to correct the behaviour.
- No change to the quote aggregation kernels or to the values the Market chart shows.
- Encoder input windows stay bar-based; a quote feature is not accepted as an encoder input.
