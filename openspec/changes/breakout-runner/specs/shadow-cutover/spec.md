# Spec Delta

## Purpose

Defines how the runner is proven against the existing Claude process before it takes over, and how the Claude triggers are retired without double-writing the tracker.

## ADDED Requirements

### Requirement: Shadow mode writes nothing live
In shadow mode the runner SHALL produce the same rows it would write but store them outside the live signal set, tagged with the config version, so they can be compared with Claude's rows for the same week.

#### Scenario: Shadow Friday
- **WHEN** a shadow run completes
- **THEN** the live `signals` table is unchanged and the shadow output is retrievable

### Requirement: Verdict comparison report
For each shadow week the system SHALL report per symbol whether verdict, trigger price, stop and volume multiple agree with Claude's rows, and SHALL list every disagreement with the rule identifiers that fired.

#### Scenario: Disagreement
- **WHEN** the runner says WAIT and Claude said ENTRY_REDUCED for a symbol
- **THEN** the report lists both with the rule that decided the runner's verdict

### Requirement: Regression fixtures from the case log
Historical cases (ACMESOLAR and logged cases) SHALL exist as fixtures with expected measured values, and the runner SHALL reproduce them within stated tolerances in CI.

#### Scenario: Fixture drift
- **WHEN** a code or config change alters a fixture value beyond tolerance
- **THEN** CI fails and shows the changed values

### Requirement: Cutover checklist
Cutover SHALL require a configured number of consecutive full-scan shadow weeks with every disagreement explained, the configured tokens verified, and both Claude triggers disabled in the same step that enables live writes.

#### Scenario: Only one trigger disabled
- **WHEN** the live switch is attempted while a Claude trigger is still enabled
- **THEN** the checklist is incomplete and live writes remain off

### Requirement: Rollback
The runner SHALL be disableable by a single switch, after which the Claude triggers can be re-enabled without data repair, because only append-only signals and idempotent rows were written.

#### Scenario: Roll back
- **WHEN** the live switch is turned off
- **THEN** no further runner writes occur and existing rows remain valid
