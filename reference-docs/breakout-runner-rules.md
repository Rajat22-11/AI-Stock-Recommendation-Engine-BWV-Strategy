# Breakout-with-Volume runner: rule specification (for the explore agent)

Author: Claude, 5 Nov 2026. Purpose: show exactly how I would build the deterministic runner, with every decision written as a numbered, testable rule, so explore can confirm, refute or refine each one instead of guessing my intent.

Companion files in `docs/`: `breakout.config.yaml` (every number), `tracker-context.md` (Supabase schema and ledger), `skills/breakout-with-volume-strategy/` (the skill), `fixtures/ACMESOLAR_weekly.csv` (real bars), `tapetide-openapi.json` (API).

## 0. Evidence legend and honesty limits

Every claim carries a tag:

| Tag | Meaning |
|---|---|
| `[PROBED]` | I called the live Tapetide data on 5 Nov 2026 and saw this. |
| `[SPEC]` | Read from the OpenAPI file only; not called. |
| `[DB]` | Read from your Supabase project (schema, config rows, SQL function bodies). |
| `[SKILL]` | Stated in the Breakout skill or its references. |
| `[DERIVED]` | My computation or inference from the above. |
| `[DECISION]` | A design choice I am making where the sources are silent. Explore should challenge these first. |
| `[UNPROBED]` | Could not be tested; must be verified before the rule is trusted. |

**Limit that matters:** the sandbox shell cannot reach `api.tapetide.com` (proxy returns 403) and I hold no REST token. All `[PROBED]` calls went through the **Tapetide MCP tools**, which read the same datasets and share the same quota, but their envelopes differ from REST (see F-01). Field values and semantics are therefore real; exact REST JSON paths are `[SPEC]`. The first implementation task must be a smoke test of each REST route with a real token (rule T-01).

---

## 1. What the live probes showed (each one drives a rule)

| ID | Finding | Tag | Consequence |
|---|---|---|---|
| F-01 | MCP and REST envelopes differ. MCP price: `{data:[{date,open,high,low,close,raw_close,volume}], meta:{window,adjustment,coverage}}`. REST per spec: `{data, meta:{as_of,route}}`; screens add `meta.total_matches`, `meta.next_cursor`. MCP technical screen uses TradingView names (`Perf.3M`, `SMA50`, absolute-rupee `market_cap_basic`); REST uses `perf_3m_pct`, `sma_50`, `market_cap_cr`. | PROBED+SPEC | Client layer must be written against REST only. Never copy MCP field names into code. |
| F-02 | Weekly bars (`interval=weekly`, `days=450`) returned 65 rows in one call, ordered oldest first. **Each bar is dated the Monday of its week**, not Friday. | PROBED | Week key = ISO Monday. Tracker `signal_date` convention is the Friday close, so a mapping is needed (D-04). |
| F-03 | The current week is included as a partial bar with **no flag**. Bar `2026-11-02` (probed Thu 5 Nov evening) had close 407.90 = Thursday's close and volume 2,415,108 = exact sum of the four daily volumes Mon-Thu (680,481+577,564+463,238+693,825). | PROBED | Partial-week detection must be done by us from the calendar (D-05). |
| F-04 | Daily bars (`days=14`) returned 11 rows including that day's bar (5 Nov, probed 22:25 IST). `meta.coverage.last_stored_date` = 2026-11-05. | PROBED | Same-evening data is available after the close; the daily call also gives "data through" date (D-06). |
| F-05 | `close` is split/bonus-adjusted; `raw_close` is unadjusted; **`volume` is not adjusted** (`volume_adjusted:false`); `dividend_adjusted:false`; `raw_close` can be null and must not be substituted. | PROBED | Volume baseline needs a corporate-action correction (D-08). |
| F-06 | Weekly request covers calendar days, not bars: 450 days gave 65 weeks. Max 2000 days; `has_more`/`next_cursor` page backwards via `end_date`. | PROBED | Fetch window is a config value in weeks, converted to days (D-02). |
| F-07 | Fundamental screen (`screens/fundamental`) with the skill's Q1 text returned `total_results: 34`; **rows include `cmp, pe, market_cap(Cr), np_qtr, qtr_profit_var, sales_qtr, qtr_sales_var, roce, down_%, 50_dma`** and `roe: null` for every row. They do not include 200 DMA, volume, D/E or pledge columns. | PROBED | Fundamental grade A/B/C/Drop is computable straight from the screen (U-05) but ROE must not be used. Volume and extension tests need bars. |
| F-08 | The MCP screen returned 10 rows per page for `limit=5` (4 pages for 34 rows). REST says `limit` default 25, max 50, over-max is rejected (400), pages by `cursor`. | PROBED+SPEC | Follow cursors until null; do not assume `limit` is honoured; Q1 is 1 REST call at `limit=50` (U-03). |
| F-09 | The screener's own diagnostic block (`filters.validation`, `unverified_predicates`) mangled cross-field predicates (`Current price > DMA 50 AND DMA 50 > DMA 200` parsed as "DMA 200 AND Market Capitalization") and the notes say "Screener still applied them". Spot check: IGIL cmp 406.95 > 50 DMA 351.05, down 3.25 < 4: consistent. | PROBED | Ignore the diagnostic block; instead re-verify every returned row on the columns we do get (U-04). |
| F-10 | Technical screen (MCP) with a loose filter matched 160 stocks and the top hits were penny/illiquid names (PRIMIND +257% 3M, KAPILRAJ at Rs 3.94). `ATR` is in rupees. Spec: max 100 rows, **no paging past `limit`**, `meta.total_matches` gives the count; `fields=` adds extras (atr_14, high_52w, perf_*). | PROBED+SPEC | Always apply price and market-cap floors; the technical screen cannot enumerate the universe, only count it and show the top 100 (U-08). |
| F-11 | Quarterly financials: `pct_changes` are **period-over-period (QoQ), not YoY** (Sales Sep-26 887 vs Jun-26 858 shows +3.4%). The newest quarter can be provisional: `EPS in Rs` for Sep 2026 was missing while Sales/Net Profit were present, with `provisional_periods:[{period:"Sep 2026", filed_on:"2026-10-28"}]`. Values are integers in Rs crore. | PROBED | YoY growth must be computed by us from period labels; need 8 quarters for 4 YoY points; handle missing EPS (F-13 rules, E-04). |
| F-12 | Promoter pledge for ACMESOLAR: `available:false`, empty trend. | PROBED | "No data" is not "zero pledge" (L-06). |
| F-13 | Nifty 500 index history: 249 daily bars for `1y`, fields `open/high/low/close/turnover_cr/pe/pb`; one bar (2026-02-01, a Sunday budget session) had null turnover/valuation. The MCP truncated the response at 25k characters; REST states responses are complete. | PROBED | Index bars can contain nulls and weekend dates; the runner must not assume Mon-Fri or non-null. |
| F-14 | REST heatmap `GET /v1/market/heatmap/{index_slug}` returns every constituent with `change_pct_1w/1m/3m/6m/1y`, `limit` max **500**, so Nifty 500 is one call. **No 9-month return exists.** | SPEC | RS formula must drop the 9-month leg (R-02). |
| F-15 | Ledger behaviour (read from SQL bodies): only non-provisional, non-superseded signals act. ENTRY/ENTRY_REDUCED become pending positions filled by a limit buy at `entry_high`; stop = fill x 0.92 and trim = fill x 1.25 are set by the ledger at fill; the ledger also stops out on daily low and trims on daily high by itself. TRIM/REDUCE/EXIT/ADD/HOLD signals dated on/after fill and before the processed day are executed at the **next session open** (EXIT sells all, REDUCE sells half, TRIM sells `trim_fraction`, ADD buys within the 10% cap); a HOLD/TRIM/etc. signal's `stop` raises the position stop if higher. | DB | The runner must never duplicate mechanical stop/trim actions; it emits signals only (A-01). |
| F-16 | Existing practice in `signals` (breakout): weekly scans write non-provisional rows incl. 74 WAIT_ALERT, 25 WAIT, 20 HOLD, 17 ENTRY(+REDUCED); mid-week rows are all `provisional=true` (38 WAIT_ALERT, 15 HOLD, 27 WAIT, 3 ENTRY/REDUCED). | DB | The runner must reproduce this rhythm (O-05). |
| F-17 | ACMESOLAR week of 14 Sep 2026 reproduces the case log exactly from real bars: pivot 430.50, close 435.25, 13w SMA 386.52, volume 19,818,031 = **2.59x** the median of the prior 20 weeks after 4-session normalisation (raw 2.07x; 1.88x vs mean), stop for fill 458.10 = 421.45. | PROBED+DERIVED | These become golden tests (T-02). It also proves the median + per-session normalisation rules are implementable from one weekly call. |

---

## 2. Architecture rules (M = module)

- **M-01 Layers (one-way dependencies):** `config` -> `client` (Tapetide REST only) -> `data` (calendar, bars, quality flags) -> `indicators` (pure) -> `rules` (pure: setup, triggers, stages, verdict) -> `writer` (Supabase). `rules` and `indicators` import nothing from `client` or `writer`; they take dataclasses and return dataclasses. This is what makes golden tests possible.
- **M-02 No literals in code:** any number that appears in the skill or this document lives in `breakout.config.yaml`. A repo check (grep for numeric literals outside `config/` and tests, excluding 0, 1, 100) runs in CI. Allowed constants: unit conversions (100, 1e7 for Rs-to-crore), weekdays.
- **M-03 Determinism:** `evaluate(symbol_inputs, config, asof_week) -> Decision` has no I/O, no clock, no randomness. The same inputs and the same config hash produce byte-identical output (sorted keys, rounded to config `output.price_decimals`).
- **M-04 Every Decision carries its evidence:** the measured numbers and the rule IDs that fired (`rules_fired: ["S1-03","S1-07"]`) go into `signals.raw` jsonb and a human line into `signals.notes`. This reproduces the skill's "show your numbers" rule.
- **M-05 Claude boundary** is a data contract, not a call: the runner writes provisional signals and a JSON "review packet" per shortlisted name; Claude (or a human) reads the packet and writes the why-grade through a superseding row (L-01..L-05).

## 3. Config governance rules (C)

- **C-01 One file, one schema:** `breakout.config.yaml` is validated at start-up by a typed schema (pydantic or JSON Schema). Unknown keys, missing keys, wrong types, or a value outside its declared range (`min`/`max` listed in `config.schema`) abort the run before any API call, with the key path in the error.
- **C-02 Version stamp:** `config_hash = sha256(canonical YAML)`; `skill_version = YYYY-MM-DD-<first 8 hex of config_hash>` where the date is the config file's commit date; inserted into `skill_versions` (`on conflict do nothing`) with `summary` listing the changed keys against the previous version. Every `runs` and `signals` row carries it. `[DECISION]` (the existing format is date + sha of SKILL.md; this keeps the format and changes the hashed object.)
- **C-03 Ownership of overlapping parameters** `[DECISION]`: parameters the **SQL ledger** reads (`paper_capital, position_cap_pct, risk_per_trade_pct, stop_cap_pct, trim_multiple, trim_fraction, reduced_size_multiple, max_open_positions, cost_roundtrip_*`) are owned by the Supabase `config` table. The YAML holds the same keys under `ledger_mirror:`; at start-up the runner compares them with the table and **aborts with a diff if they differ** (never silently overwrites). A `--sync-ledger-config` flag writes YAML values to the table after a human confirms. Everything else is YAML-only.
- **C-04 Edits:** changing a number is a pull request touching only the YAML; CI runs the golden tests against the new values and posts the changed outputs, so a tweak shows its effect before merge.
- **C-05 Secrets never in config:** `TAPETIDE_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_*` are environment variables only.

## 4. Data rules (D)

- **D-01 Calendar first (free):** call `GET /v1/market/status` (free). If `is_trading_day=false`, exit 0 with a `skipped_non_trading_day` log and no Tapetide data calls. `[SPEC]`
- **D-02 Fetch windows in weeks, converted to days:** `weekly_bars_to_fetch_weeks` (default 110, enough for 52w pivot + 20w baseline + a 104-week correction scan) -> `days = ceil(weeks*7)+7`. One `GET /v1/stocks/{symbol}/price?interval=weekly&days=...` per symbol. If `meta.window.truncated` or fewer rows than needed, page with `end_date=next_cursor` (counts toward quota); record `history_short` when the stock simply has fewer bars (new listing).
- **D-03 Holiday calendar:** `GET /v1/market/holidays?from=<Jan 1 of year-1>&to=<Dec 31 of year+1>`; cache in Supabase `config` or a repo JSON file with TTL `calendar_cache_days`. Trading sessions = Mon-Fri minus holidays, plus any special weekend sessions listed (F-13 shows a Sunday 1 Feb 2026 index bar), so the calendar must accept non-weekday sessions.
- **D-04 Week identity:** `week_key` = Monday date of the bar (Tapetide). `week_last_session` = last trading session in that Mon-Sun span from D-03. `signal_date` written to the tracker = `week_last_session` of the last completed week.
- **D-05 Completeness test:** a weekly bar is **complete** iff `data_through >= week_last_session`, where `data_through` = the latest date in the symbol's daily bars (D-06). Otherwise label `PARTIAL (n sessions)` with `n` = trading sessions from the Monday through `data_through`. Triggers are judged only on the last complete bar; the partial bar is used only for mid-week provisional rows (O-05).
- **D-06 Daily call per shortlisted symbol:** `GET /price?interval=daily&days=30` (about 20 rows): gives `data_through`, ATR(14) (needs 15 bars), 20-session median traded value, circuit heuristic, and the latest close for entry-zone maths. Skipped for symbols dropped earlier in the funnel.
- **D-07 Sessions per week** for volume normalisation = count of D-03 sessions in that week up to `min(week_last_session, data_through)`. No extra API call.
- **D-08 Corporate-action correction** `[UNPROBED on a split name]`: for each bar, `adj_factor = raw_close/close` when `raw_close` is not null. If `adj_factor` is not constant (relative spread > `corp_action_tolerance_pct`, default 0.5%) anywhere in the 20-week baseline window or the 52-week pivot window, set flag `corporate_action_in_window`, and replace volume by `volume * adj_factor` for bars before the change (shares in today's terms). If `raw_close` is null for any baseline bar, do not substitute; flag `raw_missing` and **downgrade the trigger to WAIT** unless config `allow_raw_missing_trigger=true`.
- **D-09 Nulls:** a null OHLC/volume in the window makes that bar unusable; if more than `max_null_bars` (default 1) bars in the baseline are unusable, the symbol gets `data_quality=insufficient` and verdict WAIT with the reason in notes. Never fill with zero or carry forward silently.
- **D-10 Units:** all money in Rs, market cap in Rs crore; convert at the client boundary only (`1 Cr = 1e7`). Percent fields are percent numbers (7.5 = 7.5%), never fractions.
- **D-11 Retry/idempotent reads:** GETs retry on 5xx and network errors up to `http.retries` with exponential backoff; errors are refunded by Tapetide `[SPEC]`. 429 honours `Retry-After`; if `X-RateLimit-Remaining == 0` stop the run (Q-04). 400/404 for one symbol: log, mark `symbol_error`, continue.

## 5. Indicator rules (I) - all on the last complete weekly bar `t` unless stated

Let `bars[k]` be completed weekly bars, `t` the latest.

- **I-01 Pivot:** `pivot = max(high[t-L .. t-1])`, `L = indicators.pivot_lookback_weeks` (52). The breakout bar itself is excluded.
- **I-02 Breakout close:** `close[t] > pivot` (strict). Also report whether the prior bar was already above its own pivot (`already_broke_out`), and `prior_ath_just_above` (any high in the longest history within `ath_proximity_pct` above the pivot).
- **I-03 Normalised volume:** `nv[k] = volume[k] * session_base / sessions[k]` (`session_base`=5; D-07), with D-08 applied. `baseline = median(nv[t-20 .. t-1])` (`volume_baseline: median`, 20 weeks). `vol_mult = nv[t] / baseline`.
- **I-04 SMAs:** `SMA_n[t] = mean(close[t-n+1..t])` for n in `sma_weeks` (13,26,39,52). If fewer than n bars exist the value is null and the dependent test is `unknown`, not false. `slope13 = SMA13[t] - SMA13[t-slope_lookback_weeks]`; `rising = slope13 > 0`. `dist_sma_pct = (close/SMA13 - 1)*100`. `stacked = SMA13 > SMA26 > SMA39 > SMA52`.
- **I-05 Close location:** `(close-low)/(high-low)`; null if high==low.
- **I-06 Correction (Setup A condition 1)** `[DECISION]`: over `window = bars[t-correction_lookback_weeks .. t-1]` (104) compute the **maximum drawdown** with running peak of `high` and trough of `low`: `correction_pct = (peak-trough)/peak*100`, plus `peak_date`, `trough_date`, `weeks_taken`. The skill says "from the prior all-time or 52-week high ... 40-60%+ over months"; max drawdown in the lookback is my operational reading. ACMESOLAR on 14 Sep 2026: 39.6% (peak 324.30 on 2025-09-15, trough 195.90 on 2026-01-26, 19 weeks).
- **I-07 First higher low (condition 2):** the first 3-bar pivot low after `trough_date` whose `low > trough` (pivot low: `low[k] < low[k-1] and low[k] < low[k+1]`, `pivot_low_bars=1` each side per the skill's "3-bar"). Needs one bar after k, so the last two bars cannot qualify. ACMESOLAR: 2026-03-09, low 216.34.
- **I-08 Base range:** `(max(high)-min(low))/min(low)*100` over the last `base_window_weeks` (8) completed weeks before `t`; classify tight (<=12), normal (<=20), loose (<=30), not-a-base (>30). ACMESOLAR: 24.9% (loose -> needs >=3x volume on the break per the calibration guide).
- **I-09 ATR:** daily `ATR14` = Wilder's smoothing over the last 14 true ranges from the D-06 bars; `atr_pct = ATR14/close*100`. Fallback: weekly ATR14/2.27 if daily bars are unavailable, with flag `atr_from_weekly`. (Do not use the screener's `atr_14` for the final decision; it is for pre-filtering only.)
- **I-10 Traded value:** `median(close*volume over last 20 sessions)/1e7` Rs crore. `liquidity_tier = 'thin'` if below `thin_turnover_cr` (5) else `'liquid'` (matches the tracker protocol).
- **I-11 Overextension peaks (for Trigger B):** series `d[k] = dist_sma_pct[k]`. A local peak is `d[k]` the maximum within `+-peak_window_weeks` (3) and `d[k] >= min_peak_dist_pct` (10). Take the last `overext_peaks_to_measure` (3) peaks in `overext_lookback_weeks` (104). `own_threshold = median(peak values)`; if fewer than `overext_min_peaks` (2) peaks exist, no threshold (`insufficient_history`).
- **I-12 Circuit heuristic** `[DECISION]`, because no circuit-band route exists: count sessions in the last 20 where `high==low` or (`close==high` and `change_pct>= circuit_pct_min`); if count >= `circuit_sessions_flag` (3) set `circuit_suspect` and apply the stop-executability warning (S1-12).

**Calculations added after review (previously only implied):**
- **I-13 Return windows (for RS):** `r3 = close[t]/close[t-13]-1`, `r6 = close[t]/close[t-26]-1`, `r12 = close[t]/close[t-52]-1` (offsets from `rs.window_weeks`); times 100 to match the heatmap's percent fields. Fewer bars than the offset -> that return is null and RS is `unavailable`.
- **I-14 Higher-high / higher-low structure (selection signal 3):** within the last `structure_lookback_weeks` (26), find 3-bar pivot highs and pivot lows; pass iff the last `hh_hl_pivots` (2) pivot highs are rising and the last 2 pivot lows are rising; `unknown` if fewer pivots exist.
- **I-15 Holdings trend (selection signal 6, optional, default off):** from the shareholding route, `d_promoter` and `d_inst = d_FII + d_DII` between the last two quarters; `rising` iff `d_promoter >= 0 and d_inst > 0`. If any bucket moves more than `holdings.reclass_warn_pp` (5) in one quarter, tag `possible_reclassification` and do not count it as a pass (the tool itself warns about this).
- **I-16 Short history:** fewer than `indicators.min_pivot_weeks` (26) bars -> `history_short`, verdict WAIT with reason; SMAs whose window exceeds the available bars are null and `stacked` is `unknown`.
- **I-17 Partial-week volume (mid-week rows only):** `nv_partial = volume_so_far * session_base / sessions_elapsed`, compared with the same 20-week median baseline; row is provisional and labelled `PARTIAL (n sessions)`.
- **I-18 Exposure:** `current_exposure_pct = qty_open * close / paper_capital * 100`; `rupee_risk = position_value * stop_pct/100`, shown in notes ("show the rupee risk").
- **I-19 Base-quality tag** (calibration §9) is computed from I-08 plus the volume trend inside the window (`dry_up` if mean `nv` over the window <= baseline) and used as the third ranking tie-breaker for WAIT_ALERT rows: tight+dry_up > normal > loose.

**Not computable from the API (stay with Claude/human, listed so nothing is assumed):** why-quality grade, promoter open-market selling, auditor qualification, regulatory impact, news catalyst.

## 6. Intake rules (U) - finding candidates

- **U-01 Which screen:** Q1/Q2 are defined in the **fundamental-screen** grammar because only that grammar has D/E, pledged %, "Down from 52w high" and "Volume 1week average vs 1year average". The technical screen cannot reproduce them (it has no pledge, D/E or 1-week/1-year volume), so it is used only for universe counts and RS cutoffs (R-03).
- **U-02 Query text is generated from config, not stored as a string:** each clause comes from `screens.*` keys, joined with `AND` on separate lines, so a tweak (e.g. `down_from_52w_high_lt: 3`) regenerates the query. Ratio names are validated once per release against `GET /v1/screens/fundamental/fields` (free) and the run aborts if a name is unknown.
- **U-03 Paging:** request `limit=50`, `sort=Return on capital employed`, `order=desc`; follow `meta.next_cursor` until null (keep query, sort, order unchanged) or until `screens.max_rows` is reached. Expected cost: Q1 1 call (34 rows on 5 Nov), Q2 1-2 calls.
- **U-04 Row re-verification:** for every row recompute from returned columns: `down_pct < down_from_52w_high_lt`, `cmp > 50_dma`. A mismatch increments `screen_drift`; if drift exceeds `screen_drift_max_pct` of rows, the run alerts and treats the screen result as untrusted (WATCH-only output).
- **U-05 Fundamental grade (offline, from the row):** A: ROCE >= 20 and profit var >= 20 and sales var >= 15. B: ROCE >= 12 and both var > 0. C: otherwise. **Drop** (symbol listed only): quarterly profit < 0 (`np_qtr < 0`) or profit var < -30. Missing ROCE or var -> grade `unknown` (kept, ranked last), never A/B. The grade sets rank and size notes only, never a gate (skill).
- **U-06 Post-filters on bars** (need D-02 data): `close <= 1.15*SMA13` (trigger) / `<= 1.10*SMA13` (alert); `vol_mult` within the trigger band; `atr_pct` <= 5 trigger / <= 4 alert; discard if more than 10% below the 52-week high. All thresholds from `screens.post_filters`.
- **U-07 Ranking (deterministic, stable sort):** (1) trigger state (Q1 before Q2); (2) fundamental grade A>B>C>unknown; (3) `abs(vol_mult - 2.5)` ascending (nearest the 2-3x sweet spot); (4) `dist_sma_pct` ascending; (5) symbol ascending as the final tie-break. `priority_rank` = position after sorting. Deep-verify at most `max_deep_verify` (8) names; the rest are written as WAIT_ALERT/WAIT without the optional calls.
- **U-08 Universe size context** (for the run summary only): one technical-screen call with `limit=1` and floors (`close>=min_price`, `market_cap_cr>=min_market_cap_cr`, `avg_volume_30d>=min_avg_volume`) reads `meta.total_matches`. Optional (`context_calls: true`).
- **U-09 Surveillance:** if `surveillance.check`, call `GET /v1/market/surveillance?framework=asm` and `gsm` (paged), build a symbol set once per run; membership sets `surveillance_flag` and is written into `notes` and `raw`. It never auto-blocks a trigger (the skill says "flag them"); `surveillance.block_entry: false` by default.

## 7. Relative strength rules (R)

- **R-01 Source:** one call `GET /v1/market/heatmap/{rs.universe_index}?limit=500` (default `nifty-500`; `nifty-total-market` needs a second page). Use `change_pct_3m`, `change_pct_6m`, `change_pct_1y`.
- **R-02 Formula** `[DECISION]`: the skill's weights are 0.4 (3M) + 0.2 (6M) + 0.2 (9M) + 0.2 (12M). No 9M return exists in the universe data, so the 9M leg is dropped and weights are renormalised: 3M 0.5, 6M 0.25, 12M 0.25 (config `rs.weights`; they must sum to 1.0 or C-01 aborts). Output is labelled `rs_method: "approx_3_windows"`.
- **R-03 Percentile for any candidate:** compute the same weighted score for every constituent, then `rs = 100 * count(universe_score < candidate_score) / count(universe)`. The candidate's own 3M/6M/12M returns come from its weekly bars (13, 26, 52 weeks back) so a stock outside the index still gets a percentile against the index. Constituents with a null window are excluded from the universe count.
- **R-04 Use:** `rs >= min_rs (80)` passes the signal; `>= 90` is "ideal". Below 80 is a near-miss (note it, do not disqualify; watchlist is soft). If the heatmap call failed, `rs` is `unavailable` and the signal counts as unknown, not failed.
- **R-05 Caveat to preserve in notes:** Nifty 500 percentiles overstate small-cap RS versus the whole market; the label says so.

## 8. Stage 1: Buy rules (S1)

Evaluated for every candidate that survived U-06, on the last complete weekly bar.

**Setup classification**
- **S1-01** `setup = A_base` if `correction_pct >= acceptable_correction_pct` (25). `quality_note`: >= 40 textbook, 25-40 "acceptable, shallower". `B_flag` if below 25 and `stacked` and base range <= `setup_b.max_range_pct` (20) and close within `setup_b.max_below_high_pct` (10) of the 52-week high. Otherwise `no_base`.
- **S1-02** Setup A condition 2: `first_higher_low` exists (I-07). If missing -> `conditions_1_2 = partial`, conviction down one notch (skill: lowers conviction, not void), but if `no_base` -> NO_GO.

**Trigger (strict, the only gate)**
- **S1-03** `triggered = breakout_close AND vol_mult >= volume.breakout_min_multiple (2.0) AND bar_is_complete`.
- **S1-04** `vol_mult` bands for conviction: <1.5 not a volume breakout; 1.5-2 marginal; 2-3 sweet spot; 3-5 strong; 5-10 "edge shrinks" (conviction -1); >10 weakest (conviction -1 and note). A `loose` base (range 20-30%) with `vol_mult < loose_base_volume_multiple` (3.0) lowers conviction by one notch; it is **not** a gate, because the calibration guide's "loose: demand >= 3x" sits in the section for ranking WAIT_ALERT setups, while the skill's strict trigger is only close above pivot on >= 2x `[DECISION]`. A `not-a-base` range (> 30%) is not a base: use Stage 3/4 logic if held, otherwise NO_GO.

**Risk gates (applied before verdict)**
- **S1-05** `atr_pct > half_size_max (7)` -> NO_GO (rule fires first).
- **S1-06** `atr_pct` in (5, 7] -> size multiplier 0.5, conviction -1. `thin` liquidity or `circuit_suspect` -> size multiplier 0.5.

**Verdict decision table (ordered; first match wins)**

| # | Condition | Verdict | Provisional |
|---|---|---|---|
| 1 | data_quality insufficient, or corporate-action unresolved, or `raw_missing` without override | WAIT (reason in notes) | no |
| 2 | S1-05 (ATR > 7%) | NO_GO | no |
| 3 | `no_base` and not triggered | NO_GO | no |
| 4 | triggered and setup in {A_base, B_flag} | ENTRY (mechanical; why pending, see L-01) | **yes until why-review** |
| 5 | `breakout_close` true but `vol_mult` < min | WAIT, `req_vol_mult` = 2.0 (or 3.0 if loose base), `req_vol_abs` = ceil(req * baseline * 5) | no |
| 6 | not broken out, setup valid, `0 <= (pivot-close)/pivot*100 <= alert_max_below_pivot_pct (5)` | WAIT_ALERT: `alert_trigger = pivot`, `req_vol_abs` as above | no |
| 7 | prior-week trigger not executed (signal older than `signal_valid_weeks` = 1) | WAIT_ALERT at the **new** pivot (no memory of the old signal; S1-10) | no |
| 8 | otherwise | not written (outside the watchlist) | n/a |

- **S1-07** (row 5) is the "false-start" case; `req_vol_abs` is the weekly share count needed for a full 5-session week (matches the tracker's `req_vol_abs`).
- **S1-08 Entry plan fields (row 4):** `entry_low = signal_close`; `entry_high = round(signal_close*(1+entry_zone_max_above_close_pct/100), 2)` (5%; ACMESOLAR 435.25 -> 457.01, matching the case log's "max chase 457.0"); `stop = round(signal_close*(1-stop_pct/100), 2)` with `stop_basis='cap8'` (the ledger re-sets stop = fill x 0.92 at fill; this value is the planning stop and the skip threshold); `trim_price = round(signal_close*(1+trim_pct/100), 2)`; `risk_pct = (close-stop)/close*100`; `size_pct = position_pct_of_capital * product(multipliers)`; `pivot`, `sma13`, `sma13_rising`, `dist_sma_pct`, `correction_pct`, `base_range_pct`, `close_loc`, `vol_mult` from section 5.
- **S1-09 Window:** `window_start` = next session after `signal_date` (D-03); `window_end` = the session before the next scheduled full scan (config `schedule.full_scan`).
- **S1-10 Re-entry:** a symbol with a stopped-out or exited position is evaluated like any other. The runner must not read old fills, old pivots or old exit prices when classifying (skill: "no memory of the old position").
- **S1-11 Never assume the entry price:** Stage 1 outputs a plan relative to `signal_close`; real fills come only from the ledger's `positions.fill_price`.
- **S1-12 Stop-executability warning:** when `circuit_suspect` or median traded value < `thin_turnover_cr`, add the sentence "stop is a decision level, not a guaranteed exit" to `notes` and halve size.
- **S1-13 Conviction (reported, not sized unless configured)** `[SKILL-SILENT]`: count of passing selection signals out of 6: RS>=80, `stacked`, HH/HL (last 3 pivot highs and lows rising), EPS+revenue YoY accelerating (E-03), range tight near high (I-08 <= normal and within 10% of the 52-week high), holding rising (optional). High >= `conviction.high_min` (5), Medium >= 3, else Low; notch adjustments from S1-02/04/06 apply first. `conviction.size_multiplier` defaults to 1.0 for all three because the skill does not give numbers; stored in `raw.conviction`.

## 9. Stages 2-4 rules (positions are read, never written)

- **P-01 Position state source:** `select * from positions where strategy='breakout' and status in ('open','pending')`, joined to the originating `signals` row. Only `fill_price`, `fill_date`, `qty_open`, `stop_current`, `trim_price`, `trimmed` are used. No open position -> no Stage 2-4 evaluation for that symbol (the skill: never assume a buy price).
- **A-01 Do not duplicate the ledger:** the ledger stops out on the daily low and trims at fill x 1.25 on the daily high by itself. The runner therefore never emits a TRIM for the mechanical +25% (Trigger A); it only annotates `notes` ("+25% level 572.62 reached/not reached").
- **S2-01 Trigger B (stock-specific overextension):** if `not trimmed` and `own_threshold` exists and `dist_sma_pct >= own_threshold` -> TRIM (non-provisional only on the full weekly scan). Notes carry the peaks used and the current distance. Ledger executes it at the next open with `trim_fraction`.
- **S2-02 Parabolic override:** if `dist_sma_pct` increased versus the prior week AND `vol_mult >= exit.signal1_min_volume_multiple (2.0)` AND `dist_sma_pct >= own_threshold` -> evaluate Stage 4 (S4-01) instead.
- **S3-01 Flag detection** `[SKILL-SILENT thresholds, taken from calibration §6/§9]`: a flag is the last `flag_weeks` window (min 3, max 10) ending at `t` with range <= `flag.max_range_pct` (20), price within `flag.max_below_high_pct` (10) of the 52-week high, `SMA13` rising, and average normalised volume <= the 20-week median (dry-up). `flag_high` = max high in the window.
- **S3-02 ADD (flag breakout):** `close[t] > flag_high` and `vol_mult >= max(flag.breakout_vs_flag_avg (1.5) * flag_avg_nv/baseline, 1.0)`. Never evaluated while price is inside the flag; verdict while inside = HOLD with `alert_trigger = flag_high`.
- **S3-03 ADD (13-week SMA refill):** `rising` and `abs(low[t]/SMA13[t]-1)*100 <= refill.low_within_sma_pct (2)` and `close[t] > SMA13[t]`; weekly close only, never mid-week; volume not a condition (skill backtest).
- **S3-04 ADD size:** `size_pct = min(trimmed_exposure_pct, position_pct_of_capital - current_exposure_pct)` where current exposure = `qty_open*close/paper_capital*100`. The refill is for buying back trimmed shares, not exceeding the cap; if the result is <= 0, no ADD.
- **S4-01 Signal 1 (euphoria):** `dist_sma_pct >= own_threshold` (or, without a threshold, `>= exit.fallback_extension_pct` 30) and `vol_mult >= 2.0` and distance rose versus last week -> REDUCE (ledger sells half at next open).
- **S4-02 Signal 2 (failure bar):** within `failure_lookback_weeks` (2) after a Signal 1 week: inside bar (`high<=prev_high and low>=prev_low`) or doji (`abs(close-open) <= 0.1*(high-low)`) or inverted hammer (upper wick >= 2x body, close in lower half) with `vol_mult >= 1.5` -> REDUCE.
- **S4-03 Signal 3 (distribution chop):** among the last 6 weeks, >= 3 have `range >= 1.5 * mean_range(10)` and `nv >= baseline`, and `abs(close[t]/close[t-6]-1)*100 <= chop_net_flat_pct` (3) -> REDUCE.
- **S4-04 Absorption exemption:** a down week with `volume >= 1.5*baseline` that closes in the upper third (`close_loc >= 0.667`) does not count toward signals 3 or 4.
- **S4-05 Signal 4 (capitulation):** `close[t] < higher_low_line` and `vol_mult >= 1.5` -> EXIT (act on the qualifying close; backtest: waiting one more week was worse). `higher_low_line` = low of the most recent confirmed 3-bar pivot low that is below the current price and above the base trough; it is printed as a rupee price in **every** Stage 4/HOLD note.
- **S4-06 No trailing stop** `[SKILL]`: the stop stays 8% below fill; HOLD signals carry `stop=NULL` (config `trail.enabled=false`). The ledger raises stops only if a signal supplies a higher one.
- **S4-07 Business kill switch** (promoter selling, qualified audit, regulatory hit): not computable from the API. The runner raises an **alert** (not an EXIT) when it sees: a symbol in ASM/GSM, a pledge increase quarter-over-quarter above `kill_switch.pledge_jump_pp`, or the weekly gap-down below the pivot on `vol_mult >= 2`. A human or Claude decides the EXIT.
- **S4-08 Weekly HOLD per open position** (matches existing practice, F-16): when no other verdict fires, emit HOLD with the line prices (pivot, trim level, higher-low line, flag trigger if any) in notes.

## 10. Fundamental inputs (E) - only for the shortlist

- **E-01** One call per deep-verified symbol: `GET /v1/stocks/{symbol}/fundamentals?period_type=quarter&metrics=...&limit=...` (REST) or its MCP equivalent `get_financials(section=quarterly, periods=8)` (probed). Metrics needed: sales, net profit (PAT), EPS, other income, operating cash flow (annual, separate), debt/equity.
- **E-02 YoY must be computed** (F-11): `yoy[q] = value[q]/value[q-4] - 1` using period labels ("Sep 2026" vs "Sep 2025"), never `pct_changes`. Require 8 quarters for 4 YoY points.
- **E-03 Acceleration:** "each recent quarter faster than the last": `yoy[q] > yoy[q-1]` for the last `accel_quarters` (3) quarters for both sales and EPS (fallback to net profit when EPS is missing).
- **E-04 Provisional/missing latest quarter:** if `provisional_periods` includes the latest quarter or EPS is null, compute on net profit and tag `eps_provisional`; never treat null as zero.
- **E-05 Quality flags for the Claude packet:** other income / PAT share (`other_income_share_pct` > `flag.other_income_pct` 30 -> why downgrade), OCF collapse, D/E above the screen limit, share of profit from a low base (YoY on base < `low_base_pct`).
- **E-06 Pledge** (`/v1/stocks/{symbol}/promoter-pledge`): only called when the screen row came from the technical path or the name is already held; for screen-derived names the "Pledged percentage < 10" filter already applied. `available:false` -> `pledge_status="no_data"` and it is listed under `data_gaps` (L-06).

## 11. Claude boundary rules (L)

- **L-01 Mechanical vs judgment split:** the runner decides everything numeric; Claude/human decides only: why-grade (A-D) and `why_text`, kill-switch events, ASM/GSM context, and ambiguous cases the runner marks `needs_review`.
- **L-02 Review packet:** for each ENTRY-row, write `raw.review_packet = {symbol, signal_id, numbers, flags, data_gaps, questions[]}` where `questions` are fixed templates (what drove the move? is other income > 30% of PAT? any promoter selling in open market? auditor remarks? ASM/GSM?).
- **L-03 Promotion flow** (signals are append-only): the runner inserts `ENTRY, provisional=true, why_grade=NULL`. The review step inserts a **new** row `supersedes=<id>, provisional=false`, verdict `ENTRY` (why A/B) or `ENTRY_REDUCED` (why C/D), `size_pct` halved for reduced, `why_grade`, `why_text`. The ledger only acts on the non-provisional successor.
- **L-04 Why-gate mode** (`why_gate.mode`): `claude_review` (default: nothing trades until reviewed), `auto_reduced` (the runner promotes unreviewed ENTRY rows to ENTRY_REDUCED after `why_gate.grace_hours` so the paper book is never starved), `auto_full` (testing only). `[DECISION]`, needs the owner's choice.
- **L-05 Claude is cost-bounded:** at most `max_deep_verify` packets per week; a packet is under `packet_max_chars` so the Claude pass stays small.
- **L-06 "No data" is data:** every unavailable input (pledge, RS, surveillance, raw_close, EPS) is listed in `raw.data_gaps` and in `signal_rationale.data_gaps`; none is silently treated as pass or fail.

## 12. Output mapping (O) to `signals`

- **O-01** `strategy='breakout'`, `exchange='NSE'`, `source` from the run type (O-05), `run_id`, `skill_version` (C-02).
- **O-02** Direct columns from sections 5/8: `signal_date, symbol, verdict, provisional, setup, stage, why_grade, why_text, signal_close, pivot, alert_trigger, entry_low, entry_high, stop, stop_basis, trim_price, size_pct, risk_pct, vol_mult, req_vol_mult, req_vol_abs, sma13, sma13_rising, dist_sma_pct, correction_pct, base_range_pct, close_loc, liquidity_tier, priority_rank, window_start, window_end, notes, raw`.
- **O-03 Enumerations** the DB CHECKs enforce: verdict in the 10 breakout values (underscored: `ENTRY_REDUCED`, `WAIT_ALERT`, `NO_GO`); `setup in ('A_base','B_flag','holding','other')`; `stage 1..4`; `stop_basis in (..., 'cap8', ...)`; `why_grade A-D`; `liquidity_tier liquid|thin`. A pre-insert validator mirrors these so a violation fails in tests, not in production.
- **O-04 Idempotency:** the table has no unique key. Before inserting, look up non-superseded rows with the same `(strategy, symbol, signal_date, source)`; if `raw.content_hash` matches, skip; if different, insert with `supersedes=<old id>`. Proposed (needs owner approval, no change made): unique index on `(strategy, symbol, signal_date, source) WHERE supersedes IS NULL`.
- **O-05 Run rhythm:** Friday full scan (`source='weekly_scan'`, run_type `weekly_scan`, non-provisional on complete bars); Mon-Thu `midweek_checkin` (provisional=true always; Q2 plus open-position Stage 4 watch; partial-week triggers allowed only as provisional with `PARTIAL (n sessions)` and per-session-normalised volume).
- **O-06 Prices step:** upsert `price_bars` (daily, from the day after the latest stored bar to the last complete session) for symbols with pending/open positions, new ENTRY names, and WAIT_ALERT names of the last 4 weeks, capped at `price_calls_max` (20). Transcription check: last stored close equals the fresh quote/bar close within `price_tolerance_pct` (0.05) or the run is marked partial.
- **O-07 Ledger step:** after prices, call `select process_ledger('<last complete session>')` via RPC. The runner never writes `positions/events/marks`.
- **O-08 Corporate-action guard for stored bars:** if a symbol with a pending/open position shows a changed `adj_factor` versus the stored bars, re-fetch and replace that symbol's stored bars and alert; the ledger replays from scratch each call, so history is repaired on the next `process_ledger`.

## 13. Orchestration, quota, failure rules (R/Q/E)

- **R-10 Run order:** market status -> usage check -> load config + validate -> calendar -> open positions -> surveillance sets -> Q1 -> Q2 -> RS heatmap -> rank/shortlist -> per-symbol (weekly, daily, optional financials) -> evaluate -> write run/signals -> prices -> ledger -> review row inputs -> close run.
- **Q-01 Plan before spending:** compute `planned_calls` from the shortlist size and enabled optionals; read `GET /v1/me/usage` (free); require `remaining >= planned_calls + quota.reserve_calls`. If not, shed work in this order: optional financials, optional pledge, surveillance, Q2, deep-verify count, then abort with `partial`. Open positions' weekly bars are shed last.
- **Q-02 Budget table (calls)** (`[SPEC]` route costs; 1 per successful data request): Friday full scan = Q1 1 + Q2 1-2 + heatmap 1 + holidays 0-1 (cached) + surveillance 2 + per name (weekly 1 + daily 1 + financials 1) x 8 = 24 + positions (weekly 1 + daily 1) x N_open (10 max = 20) (the `price_bars` upsert reuses the daily response, Q-03, so it costs 0 extra). Formula: `planned = q1 + q2 + heatmap + holidays + surveillance + N_names*(weekly+daily+financials) + N_open*(weekly+daily)`. Worst case (Q2 two pages, holidays uncached, 8 names, 10 open) = 1+2+1+1+2 + 24 + 20 = **51**. Typical (6 names, 4 open, holidays cached) = 1+1+1+0+2 + 18 + 8 = **31**. Usable on Free = 50 - `reserve_calls` 5 - `reserve_for_claude` 8 = **37**, so Free fits a typical Friday run but not the worst case (shedding order in Q-01 applies); Starter (400/day) fits comfortably. *(An earlier chat estimate of 45-75 double-counted the price upsert; this is the corrected figure.)* The same token also serves Claude/MCP sessions, so keep `quota.reserve_for_claude` (8).
- **Q-03 Daily bars for the tracker share the weekly call:** reuse the D-06 daily response for `price_bars` instead of calling again (saves up to 20).
- **Q-04 Quota exhausted:** HTTP 429 with `X-RateLimit-Remaining: 0` -> stop all data calls, write what exists, run `status='partial'`, Telegram alert with remaining/reset time (`X-RateLimit-Reset`, IST).
- **E-10 Failure semantics:** a failure in one symbol never aborts the run; a failure writing signals aborts before the ledger step; the run row is always closed (`finally`) with `status`, `data_calls` jsonb (counts per route), and `summary`.
- **E-11 Concurrency:** refuse to start if a `runs` row for the same `run_type` and `data_as_of` has `status='running'` younger than `run.max_minutes`.
- **E-12 Alerts:** Telegram on: partial/failed run, quota exhaustion, config-vs-ledger mismatch (C-03), screen drift (U-04), corporate-action repair (O-08), kill-switch hooks (S4-07).
- **E-13 Time:** all dates IST; Actions cron is UTC (`19:30 IST = 14:00 UTC`); GitHub schedules may run 5-30 minutes late, so the run must tolerate starting after 20:00 IST.

## 14. Test rules (T)

- **T-01 REST smoke test (first task, needs a real token):** one call per route in sections 4-7 with the symbol `ACMESOLAR`, asserting presence and type of every field this spec names; the output is committed as `fixtures/rest_smoke.json`. Any F-xx finding contradicted here is a spec bug.
- **T-02 Golden test G1 (ACMESOLAR, week 2026-09-14, from `fixtures/ACMESOLAR_weekly.csv`):** `pivot=430.50`; `close=435.25`; `breakout_close=true`; `SMA13=386.52` (+/-0.01); `dist_sma_pct=12.6`; `vol_mult=2.59` (+/-0.02, with `sessions=4` for that week and 5 for the baseline weeks); raw multiple 2.07 and mean-based 1.88 must **not** be used; `stacked=true` (SMA26 343.82, SMA39 305.61, SMA52 293.39); `close_loc=0.859`; `correction_pct=39.6`, `trough_date=2026-01-26`; `first_higher_low=216.34 (2026-03-09)`; `base_range_pct=24.9` (loose); `setup=A_base`; entry plan for fill 458.10: `stop=421.45`, `trim=572.62`; `entry_high=457.01` for the signal close; verdict row 4: `ENTRY`, `provisional=true`, conviction lowered one notch for the loose base with `vol_mult` 2.59 < 3.0 (S1-04). The original call on that week was `ENTRY (reduced)` with why grade B, D/E 3.93 and a promoter sell-down; those are judgment/flag inputs (D/E would also have failed the Q1 screen's `Debt to equity < 3`), so the runner's mechanical row plus a Claude review that downgrades it to `ENTRY_REDUCED` reproduces the historical outcome. Note this in the test so the difference is explicit.
- **T-03 Golden test G2 (same file, week 2026-10-26):** pivot = 476.90, close 404.80 -> not broken out, 15.1% below pivot, so row 8 (not written) or WAIT_ALERT only if within 5% (it is not).
- **T-04 Partial-week test:** at `data_through = 2026-11-05` the bar `2026-11-02` has `sessions=4`, `PARTIAL (4 sessions)`; triggers on it are provisional only.
- **T-05 Property tests:** (a) scaling all prices by k leaves ratios/verdict unchanged; (b) scaling a volume series by k leaves `vol_mult` unchanged; (c) inserting a holiday week with correct `sessions` changes `vol_mult` only through normalisation; (d) rank order is invariant to input row order; (e) identical input + config hash -> identical output bytes.
- **T-06 Config tests:** each config key has a test proving a change alters the output as intended (e.g. `stop_pct_below_fill: 10` -> `stop=391.73` for close 435.25); an unknown key fails validation.
- **T-07 Contract tests against Supabase** (a branch or transaction rollback): inserted rows pass every CHECK; ENTRY provisional -> superseding non-provisional row creates a pending position after `process_ledger`; a duplicate run inserts nothing new (O-04).

## 15. Open questions the explore agent must settle

1. **Loose-base handling (S1-04):** conviction-only (my default) vs a hard requirement of 3x volume vs a size reducer. The skill is ambiguous (calibration §9 is written for ranking alerts).
2. **Why-gate default** (L-04): `claude_review` vs `auto_reduced`.
3. **Max-drawdown correction definition** (I-06) and the Setup B qualifier thresholds (S1-01), which the skill leaves open.
4. **Flag and chop thresholds** (S3-01, S4-03) are my reading of "shallow", "wide-range", "roughly flat"; confirm or replace.
5. **RS without a 9-month window** (R-02) and Nifty-500 vs total-market universe.
6. **Ledger-mirror ownership** (C-03): one-way check (abort on diff) as proposed, or sync automatically?
7. **Stored bars adjusted vs raw** (O-08): `price_bars` has one close column; which series should it hold when a split lands mid-trade?
8. **Unique index on `signals`** (O-04): approve a schema change, or keep application-level dedup only?
9. **Free-tier viability** (Q-02): is the target plan Free with a trimmed Friday run, or Starter?
10. **Unprobed REST routes:** `market/status`, `market/holidays`, `market/surveillance`, REST `fundamentals` metric names (T-01 settles these).
