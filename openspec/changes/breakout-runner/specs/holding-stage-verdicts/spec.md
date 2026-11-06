# Spec Delta

## Purpose

Defines how open and pending paper positions read from the tracker are evaluated for Stage 2 (trim), Stage 3 (add) and Stage 4 (exit sequence), without ever assuming an entry price or duplicating the ledger's mechanical actions.

## ADDED Requirements

### Requirement: Position state comes only from the tracker
Stage 2-4 evaluation SHALL use only paper positions with status open or pending in the current book, read after the ledger has been processed, and SHALL NOT infer an entry price from signals. A symbol with no such position SHALL receive no Stage 2-4 verdict.

#### Scenario: No position
- **WHEN** a symbol has a signal but no open or pending position
- **THEN** no TRIM, ADD, REDUCE, EXIT or HOLD is produced for it

#### Scenario: Excluded pilot signals
- **WHEN** a position originates from a signal listed in `signal_exclusions`
- **THEN** it is ignored entirely

### Requirement: No duplication of ledger mechanics
The runner SHALL NOT emit a TRIM for the mechanical gain trigger and SHALL NOT emit stop-outs; it SHALL only annotate whether the gain level was reached.

#### Scenario: Gain level reached
- **WHEN** the high has reached the trim price
- **THEN** a HOLD note records that the ledger trim applies and no TRIM row is written for it

### Requirement: Overextension trim
When not already trimmed and the distance above the 13-week SMA is at or above the stock's own threshold, the runner SHALL emit TRIM, non-provisional only on the full scan, with the peaks used and the current distance in notes.

#### Scenario: At own threshold
- **WHEN** distance is 24 percent against a threshold of 22 percent
- **THEN** a TRIM row is written with both figures in notes

### Requirement: Add triggers
The runner SHALL emit ADD on a weekly close above the flag high with volume at or above the configured multiple of the flag average, or on a weekly close back above a rising 13-week SMA after the low came within the configured percent of it. ADD size SHALL not exceed trimmed exposure or the position cap. It SHALL never emit ADD inside a flag.

#### Scenario: Inside the flag
- **WHEN** price is inside the flag range
- **THEN** the verdict is HOLD with the flag high as the alert trigger

#### Scenario: Cap reached
- **WHEN** current exposure already equals the cap
- **THEN** no ADD is written

### Requirement: Exit sequence
The runner SHALL emit REDUCE on exit signals 1, 2 or 3 and EXIT on signal 4 (weekly close below the most recent higher low on volume at or above the configured multiple), acting on the qualifying close. A heavy down week closing in the upper third SHALL NOT count toward signals 3 or 4.

#### Scenario: Capitulation close
- **WHEN** the close is below the higher-low line on 1.8x volume
- **THEN** the verdict is EXIT and the higher-low price is in notes

#### Scenario: Absorption week
- **WHEN** a down week on heavy volume closes at 0.8 of its range
- **THEN** it is not counted

### Requirement: Weekly HOLD and no trailing stop
When no other verdict fires, an open position SHALL receive a HOLD with its line prices in notes and a null stop, because stops are not trailed.

#### Scenario: Quiet week
- **WHEN** no stage rule fires
- **THEN** a HOLD is written with stop null and pivot, trim and higher-low prices in notes

### Requirement: Kill-switch alerts, not exits
The runner SHALL flag, but not decide, business kill-switch conditions it can see (surveillance membership, pledge jump, gap below pivot on high volume) in notes and the run summary for human or review action.

#### Scenario: Gap below pivot
- **WHEN** a position gaps below the pivot on volume at or above the configured multiple
- **THEN** the run summary lists it as needing review and no EXIT is written by the runner
