# Prompt Changes Log

## 2026-07-05 - Conversational Interpret Prompt

- **What Changed**:
  - Updated the persona description to frame the AI assistant as a conversational collaborator rather than a one-shot converter.
  - Amended rule 5 to instruct the AI to populate `strategy_spec` with only what the user actually specified plus flagged standard assumptions when requests are partial or high-level, and to return at most 3 questions ordered by impact.
  - Amended rule 12 to specify that the AI should prefer targeted questions over guessing, guess-and-flag for standard parameters (under rule 6 assumptions), but ask for core intent (direction, market, style) instead of guessing.
- **Why**:
  - Better alignment with the back-and-forth conversational UI flow (WO207/WO208). Ensures that partial or high-level user requests are met with interactive clarification instead of immediate, low-accuracy guesses.
- **Token / Character Delta**:
  - Baseline system prompt length: 39138 characters.
  - Post-change system prompt length: 39490 characters.
  - Delta: +352 characters (within the ≤ +500 character budget).
