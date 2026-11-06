# Breakout-with-Volume runner

A deterministic replacement for the Claude scheduled scans: a Python job that runs the Deodhar weekly breakout process and writes verdicts into the Supabase paper tracker read by the paper-pnl UI. The plan lives in `openspec/changes/breakout-runner/`.

Status: built and tested, running in shadow mode. `shadow.live_writes` is `false`, so a run opens the database read-only and writes its would-be rows to `out/`. Cutover needs shadow Fridays compared against Claude's rows; see [docs/operations.md](docs/operations.md).

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # .venv/bin/python on Linux or macOS
```

## Secrets

Set these in a local `.env` file (copy `.env.example`; it is git-ignored) and as repository secrets in GitHub. Never put a value in the config file; the runner rejects anything that looks like a token.

| Variable | Used for |
|---|---|
| `TAPETIDE_TOKEN` | First Tapetide token (`primary`) |
| `TAPETIDE_TOKEN_2` | Second Tapetide token (`secondary`) |
| `TAPETIDE_TOKEN_3` | Third Tapetide token (`tertiary`) |
| `SUPABASE_DB_URL` | Postgres connection for the tracker. Shadow runs only read through it |
| `BREAKOUT_TEST_DB_URL` | Disposable Postgres for contract tests; **every test drops its schema**. Never point this at the tracker |

See [docs/tapetide-tokens.md](docs/tapetide-tokens.md) for how the tokens are used.

## Everyday commands

```bash
python -m breakout_runner.cli check-config     # validate the config, list token variables; no network
pytest -q                                      # all tests; database tests skip without BREAKOUT_TEST_DB_URL
python -m breakout_runner.cli run --dry-run    # today's mode, writing only to out/
python tools/check_literals.py                 # no strategy numbers outside the config
python tools/export_schema.py                  # refresh docs/breakout.config.schema.json
python tools/smoke_rest.py                     # call every route once; spends about 10 quota units
```

## Database contract tests

These run against a disposable Postgres built from `tests/db/tracker_schema.sql`, a snapshot of the live breakout tables, constraints and ledger functions. They never touch the tracker.

```bash
docker run -d --name breakout-test-db -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17-alpine
BREAKOUT_TEST_DB_URL=postgresql://postgres:test@localhost:55432/postgres pytest -q
```

## Changing a number

Every tunable lives in `config/breakout.config.yaml`. Edit it, bump `config_date`, and open a pull request; CI validates the file and reruns the golden tests. Values under `ledger_mirror` must match the Supabase `config` table, because `process_ledger` replays the whole paper book from them: changing one rewrites paper history.

## Layout

```
config/    breakout.config.yaml, the only home of tunable numbers
src/       config -> client -> data -> indicators -> intake/rules -> writer (one-way)
fixtures/  real ACMESOLAR bars for the golden tests
tools/     literal lint, schema export, REST smoke test
docs/      Tapetide OpenAPI spec, token guide, config schema
```
