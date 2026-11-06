# Spec Delta

## Purpose

Defines the single configuration file that holds every tunable number of the Breakout runner, how it is validated, versioned and reconciled with the Supabase ledger configuration, so a threshold change is a reviewable edit rather than a code change.

## ADDED Requirements

### Requirement: Single config file as the only home of tunables
The runner SHALL read every strategy, screen, risk, quota and schedule number from `breakout.config.yaml`. Strategy code SHALL NOT contain numeric literals other than 0, 1, 100 and declared unit conversions.

#### Scenario: Literal detected in strategy code
- **WHEN** the repository lint finds a numeric literal outside the config package and tests that is not in the allowed list
- **THEN** the lint fails and names the file and line

#### Scenario: Config change alters behaviour
- **WHEN** a config value such as the planning stop percentage is changed and the same inputs are evaluated
- **THEN** the output changes accordingly and a test exists proving this for that key

### Requirement: Fail-fast typed validation
The runner SHALL validate the config against a typed schema before any network call and SHALL abort with the key path when a key is unknown, missing, mistyped or outside its declared range.

#### Scenario: Unknown key
- **WHEN** the file contains a key not in the schema
- **THEN** the run aborts before any API call and reports the key path

#### Scenario: Weights must sum to one
- **WHEN** the RS weights do not sum to 1.0
- **THEN** validation fails with the offending key path

### Requirement: One key per concept
Each tunable concept SHALL have exactly one config key. Concepts shared with the ledger (stop percentage, position cap, trim fraction, reduced-size multiple) SHALL NOT be defined twice.

#### Scenario: Duplicate concept in config
- **WHEN** two keys define the same concept, for example a stop percentage under both the ledger mirror and risk sections
- **THEN** validation fails and names both keys

### Requirement: Config hash and version stamp
The runner SHALL compute a SHA-256 of the canonical config and derive a skill version of the form `runner-<config-date>-<first 8 hex>`, register it in `skill_versions` if absent, and stamp it on every run and signal it writes.

#### Scenario: Same config, same version
- **WHEN** the config content is unchanged between two runs
- **THEN** both runs carry the same skill version and the same config hash

#### Scenario: Whitespace-only edit
- **WHEN** only comments or whitespace in the file change
- **THEN** the canonical hash is unchanged

### Requirement: Ledger mirror check
The runner SHALL compare the config's ledger-mirror values with the Supabase `config` rows used by `process_ledger` and SHALL abort with a diff when any differ. It SHALL write ledger configuration only when an explicit sync flag is supplied.

#### Scenario: Mismatch aborts run
- **WHEN** the mirrored stop percentage differs from the database value
- **THEN** the run aborts before any write and reports both values

#### Scenario: Explicit sync
- **WHEN** the operator runs with the sync flag and confirms
- **THEN** the database values are updated to the config values and the change is logged

### Requirement: Secrets referenced by name only
The config SHALL reference secrets by environment-variable name and SHALL NOT contain secret values.

#### Scenario: Secret value in config
- **WHEN** a token-like value appears where an environment-variable name is expected
- **THEN** validation fails without echoing the value
