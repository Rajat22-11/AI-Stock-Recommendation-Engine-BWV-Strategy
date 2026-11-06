# Operating the runner

## What runs when

GitHub Actions fires the `run` workflow on three schedules. All times are IST.

| When | Mode | Tapetide units |
|---|---|---|
| Weekdays 19:30 | The calendar picks a full scan, a check-in or nothing | About 55-65 plus 1 per open position on the scan day; up to 25 on a check-in |
| Saturday 15:00 | Late sweep: only symbols whose bar was missing on Friday | 2 plus 1 per listed symbol; none if nothing is listed |
| Sunday 21:00 | Promotion: entries past the review grace period, in time for Monday | None |

## Running by hand

```bash
python -m breakout_runner.cli run --dry-run                     # today's mode; rows go to out/
python -m breakout_runner.cli run --dry-run --mode weekly_scan  # force a mode
python -m breakout_runner.cli run --dry-run --today 2026-11-06  # a specific date
```

In GitHub, use **Actions > run > Run workflow**. Dry run is on by default. The would-be rows are attached to the run as the `rows` artifact.

Without `SUPABASE_DB_URL` a dry run still evaluates the screens, but sees no positions.

GitHub-hosted runners have no IPv6, so the `SUPABASE_DB_URL` secret must be the Supabase **session pooler** string (`postgres.<project-ref>@aws-0-<region>.pooler.supabase.com:5432`), not the direct `db.<ref>.supabase.co` host.

## Going live and rolling back

`shadow.live_writes` in `config/breakout.config.yaml` is the single switch.

- **Off (default):** the database session is read-only, and rows go to `out/`.
- **On:** the runner writes signals, bars and reviews, and calls `process_ledger`.

To go live, set it to `true` and disable both Claude triggers in the same step, so one writer produces each run's rows.

To roll back, set it to `false` and re-enable the Claude triggers. No data repair is needed: signals are append-only.

## Rotating a Tapetide token

1. Create the new token at Tapetide's token settings page.
2. Update the GitHub secret (`TAPETIDE_TOKEN`, `_2` or `_3`) and your local `.env`.
3. Run `python -m breakout_runner.cli check-config` to confirm each variable is set.

## Recording a why-review

```bash
python -m breakout_runner.cli review --signal-id 123 --json '{"why_grade":"B","why_text":"...","catalyst":"..."}'
```

Grades A and B promote to `ENTRY`. C and D promote to `ENTRY_REDUCED` at half size. Only `why_grade`, `why_text`, `catalyst` and `events` are accepted; any other field, such as a volume multiple, is rejected. The entry window starts on the next session. Live writes must be on.

Without a review, an entry is promoted at reduced size once the grace period (`why_gate.grace_hours`) has passed.

## Comparing with Claude

```bash
python -m breakout_runner.cli compare --rows out/rows-weekly_scan-2026-11-06.json --claude claude.json
```

`claude.json` is a list of Claude's `signals` rows for the same date. The report lists each disagreement with the rule that decided the runner's verdict.

## When something fails

A failed or partial run shows in `runs.status` and `runs.summary` in the paper-pnl UI, and fails the GitHub job, so GitHub sends its failure email.

| Summary says | What to do |
|---|---|
| `ledger mirror differs` | The YAML and the database `config` table disagree. Fix the YAML, or run `sync-ledger --confirm`, which rewrites paper history |
| `All Tapetide tokens exhausted` | Quota resets at midnight IST. Add a token, or wait; the rerun is idempotent |
| `failed re-verification` | Over 10% of a screen's names failed its own conditions on bars. That screen gives no `ENTRY` this run |
| `stored close differs` | The stored bar and the fresh bar disagree, often after a split. Check the symbol |
| `Add to universe.exclude_symbols` | A new InvIT or REIT appeared. Add it to the config |
