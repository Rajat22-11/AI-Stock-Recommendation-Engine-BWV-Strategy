# Proposal

## Why

The weekly Breakout-with-Volume (Deodhar) scan and the mid-week check-in are run by Claude scheduled routines. Almost everything they do is arithmetic on weekly bars plus a handful of Tapetide calls, yet it costs a full agent session, is not reproducible, hides its thresholds inside prose prompts, and has already produced inconsistent rows (stop basis changed between weeks, duplicate same-day signals, runs left in `running`). A deterministic Python runner on GitHub Actions, driven by one config file, removes that overhead, makes every tunable number reviewable in a pull request, and leaves Claude only the judgement-only "why" review of a small shortlist.

## What Changes

- Add a Python runner (GitHub Actions, cron Mon-Fri, IST-aware) that performs the Friday full scan and the mid-week check-in with no LLM in the loop, and writes verdicts into the existing Supabase paper tracker (`strategy='breakout'`) that the paper-pnl UI already reads.
- Add one config file, `breakout.config.yaml`, validated by a typed schema; no strategy number may live in code (CI lint). Duplicate keys in the seed config are removed and rule/YAML key-name drift is reconciled.
- Add a Tapetide REST client that accepts **at least three tokens, ordered in config** (env-var names only, never values) and fails over to the next token when one is quota-exhausted; burst rate limits are handled by backoff, not failover. Per-token usage is tracked and a quota planner sheds optional work before spending.
- Fetch one daily-bar call per symbol and resample to weekly in code (exact session counts, split-adjusted prices, volume corrected using `raw_close`), replacing the weekly-plus-daily pattern.
- Implement the Deodhar rules deterministically: Setup A/B classification, strict trigger (weekly close above 52-week pivot on at least 2x per-session-normalised median volume), conviction and size modifiers, planning stop `entry_high x 0.92` with `risk_pct` against `entry_high`, Stage 2-4 for open paper positions, and WAIT/WAIT_ALERT/NO_GO.
- Rank candidates by trigger state, then base quality, then distance to the 13-week SMA, then fundamental grade.
- Write `runs`, `signals`, `signal_rationale`, `price_bars`, `trade_reviews` and `reviews` rows deterministically and idempotently, call `process_ledger` in a fixed order, honour `book_started_on` and `signal_exclusions`, and never write `positions`, `events` or `marks`.
- Keep Claude as an optional "why" review: the runner emits a review packet; by default (`auto_reduced`) unreviewed ENTRY rows are promoted to `ENTRY_REDUCED` after a grace period, so the book never depends on Claude.
- Run shadow mode against Claude's verdicts for several weeks, then disable the two Claude triggers.
- **BREAKING (operational):** after cutover the Claude Friday and Tuesday triggers (`trig_01HAFpWG929SAYtBykPMBpdr`, `trig_01MiRuCTw8i2UR2AUE3uhkiz`) and the Drive state file `breakout-scan-state.md` are retired. Running both writers together would double-log signals.
- No Telegram or other push channel: outcomes appear in `runs.status`/`runs.summary`/`reviews` (shown by the UI) and GitHub's own failure email.

## Capabilities

### New Capabilities
- `runner-config`: single typed config file, validation, config hash/versioning, ledger-mirror check, no-literals rule.
- `tapetide-access`: Tapetide REST client, ordered multi-token failover, rate-limit/quota handling, usage tracking, call budgeting.
- `market-data`: trading calendar, daily-to-weekly bars, partial-week and session-count handling, corporate-action volume correction, data-quality flags.
- `weekly-indicators`: pivot, SMAs, normalised volume multiple, ATR, correction depth, higher low, base range, overextension peaks, RS approximation.
- `screen-intake`: Q1/Q2 generation from config, paging, row re-verification, fundamental grade, ranking, shortlist, surveillance flags.
- `breakout-entry-verdicts`: Stage 1 decision table, conviction and sizing, entry plan fields and Monday action text.
- `holding-stage-verdicts`: Stage 2-4 evaluation of open paper positions from DB state (trim, add, reduce, exit, hold, kill-switch alerts).
- `midweek-checkin`: provisional week-to-date check using Friday's signals from the DB.
- `tracker-writer`: run lifecycle, idempotent signal/price writes, ledger ordering, book scoping, constraint validation.
- `tracker-reviews`: deterministic `signal_rationale`, `trade_reviews` and weekly `reviews` rows.
- `why-review`: review packet contract, supersede flow, grace-hours fallback.
- `run-scheduling`: GitHub Actions workflow, cron/IST handling, full-scan-day decision, concurrency guard, failure surfacing.
- `shadow-cutover`: dry-run mode, verdict comparison against Claude, cutover checklist.

### Modified Capabilities
<!-- None: openspec/specs is empty. The Supabase schema lives in the separate DB repo; its additions are tracked as a cross-repo dependency in design.md and tasks.md. -->

## Impact

- New Python project in this repo (config, client, data, indicators, rules, writer, CLI, tests, fixtures, workflows); no existing code is modified.
- Supabase project `moqyzjrnqzqzrxiuqyuf` (writes through service credentials), using the existing schema with no migrations. Later additions (`strategy_configs`, `runs.config_sha256`, a partial unique index on `signals`) are deferred and belong in the DB repo (`AI Stock Recommendation DB`), which would first need its 12 local migrations reconciled with the 25 live ones.
- GitHub: repository to be created, secrets for three Tapetide tokens and the Supabase connection, scheduled and manual workflows.
- Tapetide: Starter plan recommended before cutover (Friday about 31-51 units, mid-week about 15-20 per run against a 50/day Free limit shared with other consumers).
- UI (paper-pnl on Render): no code change expected; it keeps reading the same views.
