# Design

## Context

See proposal.md for motivation. Current state, as observed (live Supabase project `moqyzjrnqzqzrxiuqyuf`, 4-5 Nov 2026):

- Claude runs the Friday scan and a mid-week check-in from prompts and a skill, writing `runs`, `signals`, `signal_rationale`, `price_bars`, `trade_reviews` and `reviews`, then calling `process_ledger(date)`. The paper-pnl UI on Render reads the views.
- `process_ledger` replays the whole breakout book from `signals` and `price_bars` on every call (position ids churn). It never reads `created_at`, so a late-written successor row still fills inside its window. It sets the real stop (fill x 0.92) and trim (fill x 1.25) itself and reads capital, caps, trim fraction, reduced-size multiple and costs from the DB `config` table. A changed DB value therefore rewrites paper history.
- `signals` is append-only with no unique key; history contains same-day duplicates. Verdicts use underscore enums (`ENTRY_REDUCED`, `WAIT_ALERT`, `NO_GO`).
- The DB repo has 12 local migrations; the live DB has 25. Any new migration needs reconciliation first.
- Tapetide REST (OpenAPI 1.0.0, 110 paths). Bars: `/v1/stocks/{symbol}/price` (calendar-day windows, max 2000, cursor paging). Weekly bars are labelled by Monday, include the current partial week with no flag, and volume is not split-adjusted (`raw_close` supplied). The fundamental screen is the only grammar that can express Q1/Q2, pages by cursor, and returns `ratio_values`. Surveillance returns an empty list when its source is over a week old. No REST route for portfolio, circuit bands, T2T, series or SME. Free quota is 50 calls a day shared across REST and MCP.
- Reference material in `reference-docs/`: `breakout-runner-rules.md` (numbered rules), `breakout.config.yaml` (seed), `ACMESOLAR_weekly.csv` (fixture), `openapi.json`. Recomputing the fixture reproduced the rule document's golden values (pivot 430.50, SMA13 386.52, volume 2.587x, correction 39.6%, base range 24.9%).
- Confirmed decisions: holdings are the paper positions in the DB; planning stop is entry high x 0.92 with risk against entry high; ranking is trigger state, base quality, distance to 13-week SMA, fundamental grade; GitHub Actions hosts the runner; no push channel.

## Goals / Non-Goals

**Goals:**
- A deterministic `evaluate(inputs, config) -> decision` core with no I/O, tested against fixtures.
- Every tunable in one validated file; changing one is a reviewable pull request.
- Three-plus ordered Tapetide tokens with safe failover and visible usage.
- Idempotent, atomic writes that the existing ledger and UI consume unchanged.
- A runner that completes without Claude; Claude only enriches.

**Non-Goals:**
- Placing orders or touching real brokerage accounts.
- Tracking real holdings outside the paper book.
- Backtesting or changing the strategy's rules (thresholds are carried over and tuned later via config).
- Replacing the UI or the `process_ledger` function.
- Telegram or any other push channel.
- Using Kite or the `eod2_data` repository as fallbacks in version 1.

## Decisions

### D1. Layered package with a pure core
`config -> client -> data -> indicators -> rules -> writer`, one-way. `indicators` and `rules` import nothing from `client` or `writer` and take dataclasses, so golden and property tests need no network.
*Alternative:* a script per workflow. Rejected: duplicates logic between Friday and mid-week and blocks fixture testing.

### D2. Pydantic v2 models, `extra=forbid`, exported JSON Schema
Cross-field rules (weights sum to 1, one key per concept, ledger mirror equality) need code validators. Declared ranges live beside the field in the model, not in the YAML.
*Alternative:* JSON Schema only. Rejected: cannot express cross-field constraints cleanly. A CI check fails on numeric literals outside config and tests (allowed: 0, 1, 100, unit conversions).

### D3. Config hash as version; ledger values mirrored, not owned
Version is `runner-<date>-<sha8>` over canonical JSON. Ledger-owned values stay in the DB `config`; the YAML holds a mirror the runner compares at start and aborts on a diff. A separate explicit flag syncs them, because syncing rewrites paper history. The `skill_versions` row and `runs.notes` record the exact config, so no schema change is needed. A `strategy_configs` table and `runs.config_sha256` remain possible later additions.

### D4. One daily call per symbol, resampled in code
A daily window of about 800 calendar days gives weekly OHLCV, the true session count per week, ATR, turnover, finality (`last_stored_date`) and the rows for `price_bars`. This replaces weekly-plus-daily (two calls per name) and removes the holiday calendar from volume normalisation, so unscheduled closures and special sessions are counted correctly. The calendar is still needed for the full-scan day, window end and expected last session.
*Alternative:* weekly call plus daily call (the rule document). Rejected: about 8 to 18 extra units per Friday, and session counts depend on a calendar that can be wrong.

### D5. Ordered multi-token client
Config lists tokens as `{label, env}` in order. A `TokenPool` holds the active index and per-token remaining quota (from the free usage route at start, then decremented per charged call and re-synced on response headers). 429 `quota_exhausted` or remaining below planned need advances the pointer and retries once; `rate_limited` sleeps on the same token. Tokens are never logged; metrics use labels. The planner works on combined remaining quota minus reserves.
*Alternative:* round-robin. Rejected: the requirement is ordered failover, and round-robin spreads usage and complicates quota reasoning.

### D6. Volume band is conviction-only
The seed config applied 2-5x as a hard post-filter while the rules assigned conviction penalties above 5x, which made those branches unreachable and would have dropped real past entries (9.0x, 9.04x, 7.08x). The upper bound lowers conviction only; the lower bound (2.0x) remains the strict trigger.

### D7. Stop semantics
The planning stop is `entry_high x (1 - stop_pct)`; `risk_pct` is measured against entry high; the ledger still sets the real stop at fill. HOLD rows carry a null stop because any higher `signals.stop` raises the position's stop.

### D8. Write path and ordering
Single connection to Postgres (service credentials) so the run's inserts are one transaction up to the second ledger call. Order: open run, upsert bars, ledger, evaluate, signals and rationale, ledger, reviews, close run. Idempotency uses a content hash per (strategy, symbol, signal date, source), checked by the runner under the single-writer concurrency guard. A partial unique index on non-superseded rows remains a possible later addition.
*Alternative:* PostgREST per row. Rejected: no transaction across the sequence.

### D9. Claude boundary is a data contract
The runner never calls a model. It stores a review packet and writes provisional ENTRY rows. A review (Claude routine or human) inserts a superseding row; in default `auto_reduced` mode the runner itself promotes to `ENTRY_REDUCED` after the grace period. Successor rows set `window_start` to the session after the write because the ledger does not gate on `created_at` and would otherwise back-date fills.

### D10. Mid-week reads the database, not Drive
Friday's pivot, trigger, stop and required volume already sit in `signals`; the Drive file is retired.

### D11. Scheduling
Cron fires Monday to Friday after the close (UTC, expressed from IST in one place); the runner picks full scan, check-in or skip from the calendar. A GitHub-delayed start is tolerated. A later sweep job covers symbols whose daily bar was not final.

### D12. Shadow before live
A shadow writer stores would-be rows outside the live tables with the config version; comparison output feeds the cutover checklist. Live writes and both Claude trigger disables switch together.

## Risks / Trade-offs

- [Tapetide quota shared with MCP and other jobs; 50/day free] -> Starter plan before cutover, three-token failover, planner reserves, shedding order, usage in `data_calls`.
- [Burst rate limit undocumented] -> client limiter with a configurable value marked unverified, honour Retry-After, never failover on `rate_limited`.
- [Fixture golden value assumes 5-session baseline weeks] -> fixture carries an explicit `sessions` column; production counts real rows.
- [Rules leave thresholds undefined (Setup B, flag, chop, overextension)] -> carried as config defaults tagged decision, calibrated during shadow weeks.
- [Surveillance empty can mean stale] -> unknown state, never clear.
- [No circuit or T2T data] -> heuristic from bars, flagged as suspect; manual review for thin names.
- [Corporate action mid-trade with adjusted stored bars] -> alert in run summary and review manually; stored bars stay adjusted.
- [Late review back-dates fills] -> successor window start rule and grace-hours default.
- [Ledger replay rewrites history on any DB config change] -> mirror check aborts on a diff; sync is explicit.
- [DB repo drift] -> the runner makes no schema changes, so the 12 local and 25 live migrations stay as they are; reconcile them before any later addition.
- [GitHub schedules can be delayed or auto-disabled after inactivity] -> late-start tolerance and a keepalive/health check.
- [Two writers during transition] -> shadow mode and a single cutover step disabling both Claude triggers.
- [Look-ahead from reading partial data at midweek] -> partial bars labelled and provisional only.

## Migration Plan

1. Prerequisites: git repo with GitHub remote, three Tapetide tokens as secrets, Supabase connection secret, Starter plan, smoke test of every used route on ACMESOLAR.
2. Build in the order of tasks.md; config and client first, rules against fixtures with no network.
3. No DB repo change is required for cutover; the writer's contract tests run against a local Postgres built from the live schema.
4. Shadow weeks, compare, fix, then cutover in one step. Rollback: turn the live switch off and re-enable the Claude triggers; append-only data needs no repair.

## Open Questions

- Which Postgres connection route (direct or pooler) works from GitHub runners for the service credentials.
- The exact mid-week day set (Monday to Thursday daily versus the current Tuesday-only check-in); the config holds it and it can be settled during shadow.
- Whether the weekly `reviews` observations need a human-readable narrative beyond templated lines.
