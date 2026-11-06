# Spec Delta

## Purpose

Defines the deterministic review records the tracker UI displays beside signals and trades: rationale rows, trade reviews and the weekly review row, produced from measured data and templates without a language model.

## ADDED Requirements

### Requirement: Signal rationale rows
For every ENTRY, ENTRY_REDUCED and WAIT_ALERT signal the runner SHALL insert one rationale row with a numbers-based thesis, risks, invalidation, the rejected alternative verdict, data gaps and a confidence value from config rules. Catalyst SHALL read unverified until a review supplies it.

#### Scenario: Unreviewed entry
- **WHEN** an ENTRY is written
- **THEN** its rationale thesis cites the measured volume multiple, pivot and SMA distance and its catalyst is marked unverified

### Requirement: Data gaps are explicit
Every unavailable input (pledge, RS, surveillance, raw close, EPS) SHALL be listed in data gaps and SHALL NOT be treated as pass or fail.

#### Scenario: No pledge data
- **WHEN** pledge data is unavailable
- **THEN** it appears in data gaps and the pledge signal is unknown

### Requirement: Trade reviews for closed positions
For each paper position closed since the last run the runner SHALL insert one trade review using the tracker's computed tag, a templated lesson with the measured numbers, and author `agent`, the value the table allows for non-human rows. It SHALL NOT write a second review for the same signal and tag and SHALL NOT edit rows authored by others.

#### Scenario: Duplicate protection
- **WHEN** a review for that signal and tag already exists
- **THEN** none is inserted

### Requirement: Outcome tags for old waits
For WAIT, WAIT_ALERT and NO_GO signals at least the configured weeks old, the runner SHALL tag missed runner when the 4-week maximum gain meets the configured limit and avoided loser when the maximum drawdown meets the configured limit.

#### Scenario: Missed runner
- **WHEN** a WAIT signal's 4-week maximum gain is 18 percent and the limit is 15
- **THEN** a missed_runner review is inserted

### Requirement: Weekly review row
After each full scan the runner SHALL insert one weekly `reviews` row with statistics from the tracker views and generated observations, and SHALL leave the proposed-change field empty.

#### Scenario: Under 30 closed trades
- **WHEN** closed trades are below the configured minimum
- **THEN** the review row has no proposed change
