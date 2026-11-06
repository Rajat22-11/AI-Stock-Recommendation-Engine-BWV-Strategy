# Spec Delta

## Purpose

Defines how candidate stocks are found without a symbol list: screen queries generated from config, paged and re-verified, graded offline, ranked deterministically and trimmed to a deep-verify shortlist.

## ADDED Requirements

### Requirement: Screens generated from config
The trigger screen and alert screen SHALL be built from config predicates on the fundamental-screen grammar and SHALL NOT be stored as literal query text. Ratio names SHALL be validated against the free fields route at run start and the run SHALL abort on an unknown name.

#### Scenario: Threshold tweak
- **WHEN** the down-from-high limit changes in config
- **THEN** the generated query text reflects it without code change

#### Scenario: Unknown ratio
- **WHEN** a configured ratio name is absent from the fields route
- **THEN** the run aborts before any charged screen call

### Requirement: Paging to completion
The runner SHALL request the maximum page size, follow the next cursor with the query, sort and order unchanged until the cursor is null or the configured row cap is reached, and SHALL NOT assume a page holds the requested number of rows.

#### Scenario: Short pages
- **WHEN** a page returns fewer rows than requested but a next cursor
- **THEN** the runner continues to the next page

### Requirement: Row re-verification and drift
The runner SHALL re-check returned rows against the screen's own conditions using values recomputed from the candidate's own daily bars where a configured mapping exists and the row's named ratio values otherwise, SHALL treat a value that is still null as unknown, and SHALL treat the screen as untrusted (watch-only output) when the mismatch share of checked rows exceeds the configured limit or when no row could be checked at all.

#### Scenario: Drift above limit
- **WHEN** more than the configured percentage of rows fail re-verification
- **THEN** no ENTRY is produced from that screen and the run summary states the drift

### Requirement: Offline fundamental grade
The runner SHALL grade A, B, C or unknown from ROCE and quarterly profit and sales growth in the screen row, and SHALL drop names with negative quarterly profit or profit growth below the configured floor. The grade SHALL only affect ranking and size notes, never eligibility to be looked at.

#### Scenario: Missing ROCE
- **WHEN** a row has null ROCE
- **THEN** its grade is unknown and it ranks last among grades

### Requirement: Post-filters on bars
The runner SHALL apply the configured caps for close versus 13-week SMA, ATR and distance below the 52-week high on weekly data. The volume band SHALL be conviction-only: a multiple above the band's upper bound SHALL lower conviction and SHALL NOT remove the candidate.

#### Scenario: Volume above 5x
- **WHEN** a triggering candidate has a 9.0x volume multiple
- **THEN** it remains a candidate with reduced conviction and a note

### Requirement: Deterministic ranking
Candidates SHALL be ordered by trigger state (trigger screen before alert screen), then base quality, then distance above the 13-week SMA ascending, then fundamental grade, then symbol. At most the configured number SHALL be deep-verified; the rest SHALL be written without optional calls.

#### Scenario: Input order irrelevant
- **WHEN** the same rows arrive in a different order
- **THEN** the ranking is identical

### Requirement: Surveillance flags
The runner SHALL fetch ASM, ESM and GSM lists once per run and flag membership in notes and raw data, SHALL NOT block entries on membership by default, and SHALL treat an empty list or a stale as-of date as unknown rather than clear.

#### Scenario: Stale list
- **WHEN** the surveillance response is empty
- **THEN** each candidate's surveillance status is `unknown` and appears in data gaps
