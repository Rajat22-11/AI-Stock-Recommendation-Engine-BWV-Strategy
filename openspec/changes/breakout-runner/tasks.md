# Tasks

## 1. Prerequisites and project skeleton

- [ ] 1.1 Initialise git and a GitHub remote; verify `git remote -v` shows the origin
- [x] 1.2 Create the Python 3.12 project (src layout, pytest, ruff, pydantic, httpx, psycopg, PyYAML) and verify `pytest` runs with one placeholder test
- [x] 1.3 Copy `breakout.config.yaml`, `ACMESOLAR_weekly.csv` (add a `sessions` column) and `openapi.json` into the project's config, fixtures and docs folders; verify the files load
- [x] 1.4 Document required secrets (three Tapetide token variables, Supabase connection) in the README without values; verify the README lists each variable name
- [x] 1.5 Smoke test script that calls each route the runner uses for ACMESOLAR and writes `fixtures/rest_smoke.json`; verify it passes against a real token after quota reset and record any contradicted finding

## 2. runner-config

- [x] 2.1 Define typed config models with ranges and `extra=forbid`; verify an unknown key fails with its key path (test)
- [x] 2.2 Remove duplicate concepts and reconcile key names between the seed YAML and the rule document into one `breakout.config.yaml`; verify a test fails when two keys define one concept
- [x] 2.3 Move hidden literals (mean-range window, wick ratio, lower-half fraction, six-week windows, loose-base required multiple) into config; verify the numeric-literal lint passes on the source tree
- [x] 2.4 Implement canonical-JSON SHA-256 and the `runner-<date>-<sha8>` version; verify a whitespace-only edit keeps the hash (test)
- [x] 2.5 Implement the ledger-mirror comparison and the explicit sync command; verify the abort diff on mismatch against a stubbed DB (test)
- [x] 2.6 Add the ordered `tapetide.tokens` section (label plus env-var name, at least three) and the unverified-burst-limit key; verify a secret value in place of a name fails validation
- [ ] 2.7 Export the JSON Schema and add the CI job for validation plus the literal lint; verify CI fails on an injected literal

## 3. tapetide-access

- [x] 3.1 Implement the HTTP client with redaction, timeouts, backoff and 5xx/network retry; verify with a mock server including 503 Retry-After
- [x] 3.2 Implement the token pool with ordered failover on `quota_exhausted` and backoff-only handling of `rate_limited`; verify with mock responses that token 2 is used only after token 1 is exhausted
- [x] 3.3 Implement usage reading (free route), per-token counters and `data_calls` output; verify no token value appears in logs or output (test)
- [x] 3.4 Implement the quota planner and shedding order; verify a short-quota scenario sheds in the configured order and never sheds open-position bars first
- [x] 3.5 Implement typed endpoint wrappers (status, holidays, price, fundamental screen and fields, heatmap, index levels, surveillance, fundamentals, signals) against REST field names; verify contract tests against saved responses
- [x] 3.6 Write client docs for token setup and failover behaviour; verify the documented dry-run command works

## 4. market-data

- [x] 4.1 Implement the cached trading calendar (including special sessions) and full-scan-day logic; verify the Friday-holiday-moves-to-Thursday case (test)
- [x] 4.2 Implement daily-to-weekly resampling with real session counts and Monday week keys; verify the SONACOMS-style holiday-short week sums (test)
- [x] 4.3 Implement complete versus partial week labelling from latest stored date; verify the 5 Nov 2026 `PARTIAL (4 sessions)` case (test)
- [x] 4.4 Implement raw-close volume correction, `raw_missing` and null-bar handling, and the insufficient-history state; verify a bonus-inside-baseline test
- [x] 4.5 Implement cursor paging for the long window; verify against a mocked multi-page response

## 5. weekly-indicators

- [x] 5.1 Implement pivot, breakout close, normalised median volume multiple, SMAs, slope and stack; verify the ACMESOLAR 14 Sep 2026 golden values within tolerance
- [x] 5.2 Implement correction depth, first higher low, base range, close location and Wilder ATR; verify the golden correction (39.6%, 2026-01-26) and higher low (216.34)
- [x] 5.3 Implement overextension peaks, structure test and the optional holdings-trend signal; verify insufficient-history behaviour (test)
- [x] 5.4 Implement the RS approximation using heatmap returns and the candidate's own returns; verify a null window yields `unavailable` (test)
- [x] 5.5 Add property tests for price scale, volume scale, holiday normalisation and input order; verify they pass

## 6. screen-intake

- [x] 6.1 Generate Q1 and Q2 from config predicates and validate ratio names against the fields route; verify a config tweak changes the query text and an unknown name aborts (tests)
- [x] 6.2 Implement paging, row cap and re-verification with null-as-unknown and the drift threshold; verify drift above the limit yields watch-only output
- [x] 6.3 Implement the offline fundamental grade and the drop rules; verify grade cases including missing ROCE
- [x] 6.4 Implement post-filters with the conviction-only volume band; verify a 9.0x candidate survives with lower conviction
- [x] 6.5 Implement the ranking (trigger state, base quality, SMA distance, grade, symbol) and the deep-verify cut; verify order independence (test)
- [x] 6.6 Implement surveillance fetch (ASM, ESM, GSM) with stale and empty as unknown; verify an empty list yields `unknown` (test)

## 7. breakout-entry-verdicts

- [x] 7.1 Implement setup classification and conviction and size modifiers; verify Setup B and the ATR half-size cases
- [x] 7.2 Implement the ordered decision table with rule identifiers; verify one test per table row
- [x] 7.3 Implement planning prices (entry zone, entry-high-based stop, trim, risk against entry high) and the Monday plan template; verify the ACMESOLAR plan (entry high 457.01, stop 420.45)
- [x] 7.4 Implement the evidence payload and stop-executability wording; verify the byte-identical output property for identical inputs and config

## 8. holding-stage-verdicts and midweek-checkin

- [x] 8.1 Implement position-state reading from open and pending paper positions with book scoping and exclusions; verify excluded and absent positions yield no verdict
- [x] 8.2 Implement the overextension trim and parabolic override without duplicating ledger mechanics; verify the gain-level annotation test
- [x] 8.3 Implement flag detection, flag-breakout ADD, SMA-refill ADD and the cap logic; verify never-add-inside-flag and cap-reached tests
- [x] 8.4 Implement exit signals 1 to 4, the absorption exemption, HOLD with null stop and kill-switch notes; verify one test per signal
- [x] 8.5 Implement the mid-week job: read Friday signals, week-to-date measures, run-rate, distribution flags, near-trigger list and 52-week-high detection; verify the run-rate 40%-at-2-of-5 case
- [x] 8.6 Implement the mid-week budget and shedding; verify new-high detection is shed first

## 9. tracker-writer and tracker-reviews

- [x] 9.2 Implement run lifecycle with the concurrency guard and always-close behaviour; verify a simulated crash closes the run as failed
- [x] 9.3 Implement constraint validators mirroring the table CHECKs; verify invalid enum values are rejected before any insert
- [x] 9.4 Implement idempotent signal writes with content hash and supersede on change; verify a rerun inserts nothing (contract test on local Postgres built from the live schema)
- [x] 9.5 Implement price-bar upsert with the transcription check, call cap and ledger ordering; verify the second ledger call is skipped when signals fail
- [x] 9.6 Implement deterministic `signal_rationale`, trade reviews, outcome tags and the weekly `reviews` row; verify no duplicate (signal, tag) and no edits to other authors' rows

## 10. why-review

- [x] 10.1 Implement the review packet with size limit; verify truncation order within the limit
- [x] 10.2 Implement the supersede flow with halved size for grades C and D and the next-session window start; verify a ledger test on local Postgres that a Wednesday review does not fill on Monday
- [x] 10.3 Implement the gate modes and the grace-hours promotion; verify `auto_reduced` promotes after the grace period and `review_required` does not (tests)

## 11. run-scheduling

- [ ] 11.1 Write the GitHub Actions workflow (weekday UTC cron, dispatch with dry-run, secrets, concurrency group) and verify a dispatched dry run uploads the would-be rows
- [x] 11.2 Implement the CLI entry that selects full scan, check-in or skip from the calendar; verify each path with a faked clock
- [x] 11.3 Implement the late-bar sweep and non-zero exit on partial or failed runs; verify the failure shows in `runs.status` and the job result
- [ ] 11.4 Add the PR workflow running validation, lint and golden tests with a changed-output summary; verify a config tweak PR shows its effect
- [x] 11.5 Document operations (tokens rotation, running dry run, rollback switch); verify the documented commands work as written

## 12. shadow-and-cutover

- [ ] 12.1 Implement the shadow writer and the comparison report against Claude's rows; verify with one past week replayed from the database
- [x] 12.2 Add case-log fixtures beyond ACMESOLAR with expected values; verify CI reproduces them within tolerance
- [ ] 12.3 Run the configured number of shadow Fridays and mid-week runs and record each disagreement with its rule id; verify every disagreement is explained
- [ ] 12.4 Verify Starter plan and all three tokens before cutover; verify a failover rehearsal succeeds with token 1 deliberately exhausted
- [ ] 12.5 Perform cutover: enable live writes and disable both Claude triggers together; verify exactly one writer produces the next run's rows
- [ ] 12.6 Rehearse rollback with the live switch; verify no runner writes occur afterwards

## Workflow follow-up

- Deferred DB additions (`strategy_configs`, `runs.config_sha256`, partial unique index on signals): reconcile the DB repo's migrations first if these are ever wanted.
- Review the Setup B, flag and chop default thresholds against shadow results and tune via config pull requests.
- Archive this change after cutover is verified.
