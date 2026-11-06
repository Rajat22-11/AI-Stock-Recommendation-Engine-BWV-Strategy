# Spec Delta

## Purpose

Defines the deterministic Stage 1 decision table that turns measured indicators into one verdict tag per candidate, with sizing, planning prices and a Monday action plan, using the Deodhar rules.

## ADDED Requirements

### Requirement: Setup classification
A candidate SHALL be Setup A when correction depth is at or above the configured acceptable depth, Setup B when below it with stacked SMAs, a base range within limit and close within the configured distance of the 52-week high, and otherwise no base. A missing higher low SHALL lower conviction, not void Setup A.

#### Scenario: Shallow correction in an uptrend
- **WHEN** correction is 18 percent, SMAs are stacked, base range is 15 percent and close is 4 percent below the high
- **THEN** the setup is B_flag

### Requirement: Strict trigger is the only gate
A candidate SHALL be triggered only when the last complete weekly close is above the pivot and the normalised volume multiple is at or above the configured minimum. Partial bars SHALL never produce a final trigger.

#### Scenario: Breakout on light volume
- **WHEN** the close is above the pivot and the multiple is 1.4
- **THEN** the verdict is WAIT with the required multiple and absolute weekly volume stated

### Requirement: Second-week breakouts
When the configured switch is on, a candidate whose previous completed week already closed above its pivot SHALL NOT produce an ENTRY and SHALL be written as WAIT with the rule identifier, matching the backtest's exclusion of such signals.

#### Scenario: Already above the pivot last week
- **WHEN** a qualifying week follows a week that also closed above its pivot
- **THEN** the verdict is WAIT, with no entry plan

### Requirement: Ordered decision table
The runner SHALL evaluate in fixed order and take the first match: data quality failure gives WAIT; ATR above the NO-GO limit gives NO_GO; no base and not triggered gives NO_GO; triggered with a valid setup gives ENTRY; close above pivot on light volume gives WAIT; within the alert distance below the pivot gives WAIT_ALERT; otherwise nothing is written.

#### Scenario: Within alert distance
- **WHEN** the close is 3 percent below the pivot and the setup is valid
- **THEN** the verdict is WAIT_ALERT with the pivot as alert trigger

#### Scenario: Far from the pivot
- **WHEN** the close is more than the alert distance below the pivot
- **THEN** no row is written for the symbol

### Requirement: Conviction and size modifiers
Conviction SHALL count passing selection signals and SHALL be reduced by loose base with low volume, volume above the strong band, missing higher low and ATR in the half-size band. Size SHALL be halved for ATR in the half-size band, thin liquidity or suspected circuit behaviour. A loose base SHALL lower conviction and SHALL NOT be a gate.

#### Scenario: ATR 6 percent
- **WHEN** ATR percent is 6 and the candidate is triggered
- **THEN** the size multiple is 0.5 and conviction is one notch lower

### Requirement: Planning prices
For an ENTRY the runner SHALL set entry low to the signal close, entry high to the close plus the configured zone percent, the planning stop to entry high less the configured stop percent, the trim price from the configured trim percent, and risk percent against entry high. The ledger remains the owner of the real stop at fill.

#### Scenario: ACMESOLAR plan
- **WHEN** the signal close is 435.25 and the zone and stop are 5 and 8 percent
- **THEN** entry high is 457.01 and the planning stop is 420.45

### Requirement: Provisional until reviewed
A new ENTRY SHALL be written provisional with no why grade and SHALL not affect the ledger until a non-provisional successor exists.

#### Scenario: Unreviewed entry
- **WHEN** an ENTRY row is written by the runner
- **THEN** `provisional` is true and `why_grade` is null

### Requirement: Evidence and Monday plan
Every verdict SHALL carry the measured numbers, the rule identifiers that fired and a templated action plan (entry zone, stop, trim, alert levels and the no-chase rule above entry high) in the signal's raw data and notes. Stop-executability wording SHALL be added for thin or circuit-suspect stocks.

#### Scenario: Thin stock
- **WHEN** median traded value is below the thin threshold
- **THEN** notes state the stop is a decision level, not a guaranteed exit
