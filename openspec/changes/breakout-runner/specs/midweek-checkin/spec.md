# Spec Delta

## Purpose

Defines the mid-week check-in: a provisional, week-to-date review of Friday's watchlist and open positions that reads its state from the tracker instead of a file, and never runs the full-market intake.

## ADDED Requirements

### Requirement: State read from Friday's signals
The check-in SHALL read the latest full-scan signals (entries, alerts, waits, holds) from the database, including pivot, trigger, stop and required weekly volume, and SHALL NOT depend on any external state file.

#### Scenario: No Friday signals
- **WHEN** no full-scan signals exist within the configured age
- **THEN** the summary says so and the check-in covers open positions only

### Requirement: Week-to-date measures
For each name the check-in SHALL compute the week's high, low and last close, percent distance to trigger and stop, volume so far against required volume as a percentage and as a run-rate projection from elapsed sessions, whether the trigger was crossed and on what volume, and distribution flags defined by config.

#### Scenario: Run-rate projection
- **WHEN** 2 of 5 sessions have elapsed with 40 percent of required volume
- **THEN** the projection is 100 percent of required volume

#### Scenario: Distribution flag
- **WHEN** a name closes in the bottom of the week's range after a breakout on heavy volume
- **THEN** the distribution flag is set per the configured limits

### Requirement: Provisional output
Every mid-week row SHALL be provisional, labelled with its elapsed session count, except stop-based actions the strategy treats as immediate, and SHALL NOT create, edit or supersede Friday's rows.

#### Scenario: Trigger crossed on Wednesday
- **WHEN** a Friday name crosses its trigger on Wednesday
- **THEN** a provisional row labelled PARTIAL with 3 sessions is written and Friday's row is untouched

### Requirement: Near-trigger and new-high detection
The check-in SHALL list names within the configured percent of their trigger, and SHALL use the end-of-day signals route for 52-week-high names not on Friday's list, marking them as not from Friday's list.

#### Scenario: Within 3 percent
- **WHEN** a name is 2.4 percent below its trigger
- **THEN** it appears in the near-trigger list with the alert level

### Requirement: Bounded cost
The check-in SHALL NOT run the screens and SHALL plan its calls within the configured mid-week budget, using one daily-bar call per tracked name.

#### Scenario: Budget shed
- **WHEN** the budget would be exceeded
- **THEN** new-high detection is shed first and open positions are never shed
