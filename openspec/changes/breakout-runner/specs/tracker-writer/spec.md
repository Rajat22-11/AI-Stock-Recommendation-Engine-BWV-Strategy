# Spec Delta

## Purpose

Defines how the runner records its work in the Supabase paper tracker: run lifecycle, idempotent signal and price writes, ledger ordering and book scoping, so reruns and failures never corrupt the paper book.

## ADDED Requirements

### Requirement: Run lifecycle always closed
Each run SHALL insert a `runs` row with the run type, trigger identifier from the CI run, data-as-of date and config-derived skill version, and SHALL close it with status, per-route call counts and a summary even when the run fails.

#### Scenario: Crash mid-run
- **WHEN** an unhandled error occurs after the run row is inserted
- **THEN** the row is closed as failed with the error summary

### Requirement: Concurrency guard
The runner SHALL refuse to start when a run of the same type and data date is still running and younger than the configured maximum minutes, and SHALL close its own older running rows as failed, leaving rows from other writers untouched.

#### Scenario: Duplicate trigger
- **WHEN** a second job starts while the first is running
- **THEN** the second exits without writing and logs the reason

### Requirement: Idempotent signal writes
The runner SHALL NOT insert a signal duplicate: for the same strategy, symbol, signal date and source it SHALL skip when the content hash matches and SHALL insert a superseding row when content differs. It SHALL never update or delete signals.

#### Scenario: Rerun same data
- **WHEN** the run is repeated with identical inputs and config
- **THEN** no new signal rows are inserted

#### Scenario: Changed content
- **WHEN** a rerun produces different content for an existing key
- **THEN** a new row with `supersedes` pointing to the old row is inserted

### Requirement: Constraint validation before insert
Each row SHALL be validated against the table's enumerations (verdict, setup, stage, stop basis, why grade, liquidity tier) before insert, and a violation SHALL fail in tests or abort the write, never reach the database.

#### Scenario: Invalid verdict spelling
- **WHEN** a row uses `NO-GO` instead of `NO_GO`
- **THEN** validation rejects it before any insert

### Requirement: Fixed ledger order and ownership
The runner SHALL upsert price bars, process the ledger to compute fresh state, evaluate, insert signals, process the ledger again, then write reviews and close the run. It SHALL NOT write positions, events or marks.

#### Scenario: Signals failed to write
- **WHEN** inserting signals fails
- **THEN** the second ledger call is not made and the run closes as failed

### Requirement: Book scoping
The runner SHALL operate only on the current paper book defined by `book_started_on` and SHALL ignore signals in `signal_exclusions`.

#### Scenario: Pilot book signal
- **WHEN** an excluded signal exists for a symbol
- **THEN** it is not re-logged, superseded or managed

### Requirement: Price bar upsert and transcription check
The runner SHALL upsert daily bars for symbols with pending or open positions, new entries and recent alert names within the configured call cap, and SHALL mark the run partial when the latest stored close differs from the fresh bar beyond the configured tolerance.

#### Scenario: Close mismatch
- **WHEN** the stored close differs from the fresh close by more than the tolerance
- **THEN** the run is marked partial and the symbol is listed

### Requirement: Late review timing
A review-created successor row SHALL set its window start to the session after the review time so the ledger cannot back-date a fill.

#### Scenario: Review on Wednesday
- **WHEN** a successor row is written on Wednesday evening
- **THEN** its window start is Thursday
