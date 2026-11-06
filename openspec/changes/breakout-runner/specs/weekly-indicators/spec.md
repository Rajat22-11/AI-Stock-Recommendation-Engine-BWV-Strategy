# Spec Delta

## Purpose

Defines the pure, deterministic indicator calculations on weekly bars that the Deodhar rules depend on, so each measured number can be reproduced and tested against fixtures.

## ADDED Requirements

### Requirement: Pivot and breakout close
The pivot SHALL be the highest weekly high of the configured number of weeks before the signal week, excluding the signal week. A breakout close SHALL mean the signal week's close is strictly above the pivot.

#### Scenario: ACMESOLAR week of 14 Sep 2026
- **WHEN** the fixture is evaluated for that week
- **THEN** the pivot is 430.50, the close is 435.25 and the breakout close is true

### Requirement: Normalised volume multiple
The volume multiple SHALL be the week's volume scaled to the configured session base divided by the median of the prior 20 completed weeks, each scaled by its own session count. Mean-based and unscaled multiples SHALL NOT be used.

#### Scenario: Holiday-short signal week
- **WHEN** the signal week has 4 sessions and the baseline weeks have 5 each
- **THEN** the multiple for the ACMESOLAR fixture is 2.59 within 0.02, not 2.07 or 1.88

#### Scenario: Scale invariance
- **WHEN** all volumes are multiplied by a constant
- **THEN** the multiple is unchanged

### Requirement: Moving averages, slope and stack
The runner SHALL compute the configured weekly SMAs, the slope of the 13-week SMA over the configured lookback, distance of close from it, and whether the SMAs are stacked. A value without enough bars SHALL be unknown, not false.

#### Scenario: Fixture values
- **WHEN** the ACMESOLAR fixture is evaluated for 14 Sep 2026
- **THEN** SMA13 is 386.52, SMA26 343.82, SMA39 305.61, SMA52 293.39 and distance is 12.6 percent

### Requirement: Correction depth and first higher low
Correction depth SHALL be the latest drawdown from a running peak of highs to the following trough of lows that reaches the configured acceptable depth within the configured lookback, falling back to the deepest drawdown when none reaches it, reported with peak and trough dates. The first higher low SHALL be the first 3-bar weekly pivot low after the trough that is above the trough.

#### Scenario: Fixture correction
- **WHEN** the ACMESOLAR fixture is evaluated
- **THEN** the correction is 39.6 percent from 2025-09-15 to 2026-01-26 and the first higher low is 216.34 on 2026-03-09

#### Scenario: No higher low yet
- **WHEN** no later pivot low exceeds the trough
- **THEN** the higher low is absent and reported as missing

### Requirement: Base range, close location and ATR
Base range SHALL be computed over the configured weeks before the signal week and classified as tight, normal, loose or not-a-base by config bands. Close location SHALL be null when high equals low. ATR SHALL be Wilder ATR over the configured period from daily bars, expressed as percent of close, with a flagged weekly fallback.

#### Scenario: Loose base
- **WHEN** the base range is 24.9 percent
- **THEN** it is classified loose

#### Scenario: Zero-range week
- **WHEN** high equals low
- **THEN** close location is null

### Requirement: Overextension peaks
The runner SHALL derive a stock-specific overextension threshold from the configured number of recent local peaks of distance above the 13-week SMA, and SHALL report no threshold when fewer than the configured minimum peaks exist.

#### Scenario: Insufficient history
- **WHEN** fewer than two qualifying peaks exist
- **THEN** the threshold is absent and marked `insufficient_history`

### Requirement: Relative strength approximation
RS SHALL be a percentile of the candidate's weighted 3, 6 and 12 month return against the benchmark universe computed with the same weights, labelled approximate, and SHALL be unavailable rather than failed when inputs are missing.

#### Scenario: Heatmap unavailable
- **WHEN** the universe call fails
- **THEN** RS is `unavailable` and the RS signal counts as unknown
