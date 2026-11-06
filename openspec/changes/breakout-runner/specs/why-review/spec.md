# Spec Delta

## Purpose

Defines the narrow boundary where Claude or a human supplies judgement the API cannot: the why grade and event checks. The runner works without it, and the boundary is a data contract rather than a call.

## ADDED Requirements

### Requirement: Review packet per entry
For each ENTRY row the runner SHALL store a compact review packet (symbol, signal id, numbers, flags, data gaps and fixed questions) limited to the configured size.

#### Scenario: Packet size
- **WHEN** a packet would exceed the configured character limit
- **THEN** lower-priority fields are dropped and the packet stays within the limit

### Requirement: Supersede flow
A review SHALL promote an entry by inserting a new non-provisional row that supersedes the provisional one, with verdict ENTRY for why grades A or B and ENTRY_REDUCED with halved size for C or D, plus grade and text. The original row SHALL never be modified.

#### Scenario: Grade C
- **WHEN** a review assigns grade C
- **THEN** a successor ENTRY_REDUCED row with half the size supersedes the provisional ENTRY

### Requirement: Default without review
With the default gate mode, the runner SHALL promote an unreviewed provisional ENTRY to a non-provisional ENTRY_REDUCED with no grade after the configured grace hours, so the paper book does not depend on a review.

#### Scenario: Grace expires
- **WHEN** no review arrives within the grace period
- **THEN** a successor ENTRY_REDUCED row is written with window start on the next session

### Requirement: Mode selection
The gate mode SHALL be one of review-required, auto-reduced or auto-full from config, with auto-reduced as the default and auto-full documented as test-only.

#### Scenario: Review-required mode
- **WHEN** the mode is review-required and no review exists
- **THEN** no promotion occurs and the run summary lists the pending entries

### Requirement: Judgement-only scope
The review SHALL be limited to why grade, why text, news catalyst, auditor or regulatory events and promoter open-market selling; every numeric decision SHALL remain with the runner.

#### Scenario: Review changes a number
- **WHEN** a review row attempts to change a measured value such as volume multiple
- **THEN** the writer rejects it
