# Spec Delta

## Purpose

Defines how trading calendar, daily bars and weekly bars are obtained and cleaned so downstream rules see exact session counts, complete-week flags, corrected volume and explicit data-quality states.

## ADDED Requirements

### Requirement: Trading-day gate
The runner SHALL call the free market status route first and SHALL exit successfully without data calls when the day is not a trading day.

#### Scenario: Holiday
- **WHEN** status reports `is_trading_day` false
- **THEN** the run logs `skipped_non_trading_day` and makes no charged call

### Requirement: Full-scan day from the calendar
The runner SHALL treat a session as the full-scan day when it is the last trading session of its week according to the cached holiday calendar, including a Thursday before a Friday holiday.

#### Scenario: Friday holiday
- **WHEN** Friday is a market holiday and today is Thursday
- **THEN** today is the full-scan day and Friday is not

#### Scenario: Special weekend session
- **WHEN** the calendar lists a special session on a weekend
- **THEN** it is accepted as a session

### Requirement: One daily call per symbol resampled to weekly
The runner SHALL fetch daily bars once per symbol over a configured window and derive weekly bars by grouping rows by Monday-based week, using the actual number of daily rows as the session count for each week.

#### Scenario: Holiday-short week
- **WHEN** a week has four daily rows
- **THEN** its session count is 4 and its volume is the sum of those rows

#### Scenario: Window longer than the API cap
- **WHEN** the history needed exceeds one response
- **THEN** the runner follows the paging cursor until the configured history is covered or the history ends

### Requirement: Complete versus partial week
A weekly bar SHALL be complete only when the latest stored daily date is at or after the week's last scheduled session; otherwise it SHALL be labelled partial with its elapsed session count.

#### Scenario: Thursday evening
- **WHEN** data runs through Thursday and Friday is a scheduled session
- **THEN** the current week is partial with 4 sessions and no trigger is final on it

#### Scenario: Late data for a symbol
- **WHEN** a symbol's latest stored date is earlier than the last session
- **THEN** that symbol is marked not final and is retried by the late-bar sweep

### Requirement: Corporate-action volume correction
Volume SHALL be corrected by `raw_close / close` per bar when the factor is not constant in the baseline or pivot window, and the symbol SHALL be flagged. A null `raw_close` SHALL NOT be substituted; the symbol SHALL be flagged and any trigger downgraded to WAIT unless config allows it.

#### Scenario: Bonus inside baseline
- **WHEN** a bonus issue changes the adjustment factor within the 20-week baseline
- **THEN** earlier volumes are expressed in today's share terms and the flag `corporate_action_in_window` is set

#### Scenario: Missing raw close
- **WHEN** `raw_close` is null for a baseline bar
- **THEN** the flag `raw_missing` is set and the trigger is downgraded

### Requirement: Data-quality states
The runner SHALL mark a symbol `insufficient` when more than the configured number of baseline bars are unusable or history is shorter than the configured minimum, and SHALL never fill nulls with zero or carry values forward.

#### Scenario: New listing
- **WHEN** a symbol has fewer weekly bars than the configured minimum
- **THEN** it is marked `history_short` and its verdict is WAIT with that reason
