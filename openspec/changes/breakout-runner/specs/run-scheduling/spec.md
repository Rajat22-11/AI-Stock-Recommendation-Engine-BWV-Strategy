# Spec Delta

## Purpose

Defines when and how the runner executes on GitHub Actions, including IST-aware scheduling, the full-scan versus check-in decision, manual dry runs and how failures become visible without a push channel.

## ADDED Requirements

### Requirement: Weekday cron with runtime decision
The workflow SHALL be triggered by a UTC cron on Monday to Friday after market close and SHALL decide at runtime, from the trading calendar and config, whether the day is the full-scan day, a check-in day or a skipped day.

#### Scenario: Normal week
- **WHEN** the workflow fires on Friday after the close
- **THEN** it runs the full scan

#### Scenario: Late start
- **WHEN** GitHub starts the job up to 30 minutes late
- **THEN** it still runs correctly using IST dates

### Requirement: Manual dispatch and dry run
The workflow SHALL support manual dispatch with a dry-run option that evaluates everything and writes nothing to the tracker.

#### Scenario: Dry run
- **WHEN** dispatched with dry-run enabled
- **THEN** no database writes occur and the would-be rows are output as an artifact

### Requirement: Config edits take effect on merge
The scheduled job SHALL use the config on the default branch at checkout, and pull requests SHALL run validation and golden tests showing the effect of a changed value.

#### Scenario: Merged tweak
- **WHEN** a config edit is merged
- **THEN** the next scheduled run uses it and stamps the new version

### Requirement: Failure visibility without push alerts
A failed, partial or quota-exhausted run SHALL be visible in `runs.status` and `runs.summary` for the UI and SHALL fail the CI job so GitHub's failure notification fires. No external messaging service SHALL be required.

#### Scenario: Quota exhausted on all tokens
- **WHEN** every token is exhausted
- **THEN** the run is `partial`, the summary states the reset time and the job exits non-zero

### Requirement: Promotion sweep
A configured run after the review grace period and before the next session SHALL promote due entries without any Tapetide call, so a Friday entry can fill on Monday.

#### Scenario: Weekend promotion
- **WHEN** the sweep runs on Sunday evening and a Friday entry is past its grace period
- **THEN** its successor row has a window start on Monday and no quota is spent

### Requirement: Late-bar sweep
A configured follow-up run SHALL retry only symbols whose daily data was not final at the full-scan run and SHALL supersede affected rows through the normal idempotent path.

#### Scenario: Straggler symbol
- **WHEN** a symbol's bar arrives after the evening run
- **THEN** the sweep evaluates it and writes a superseding row if the verdict changes
