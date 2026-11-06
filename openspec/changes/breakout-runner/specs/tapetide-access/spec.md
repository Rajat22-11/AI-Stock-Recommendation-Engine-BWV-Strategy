# Spec Delta

## Purpose

Defines how the runner talks to the Tapetide REST API: ordered multi-token failover, quota awareness, retry and rate-limit behaviour, and a call budget that sheds optional work before the daily limit is spent.

## ADDED Requirements

### Requirement: Ordered multiple tokens from config
The runner SHALL accept an ordered list of at least three Tapetide token sources in config, each naming an environment variable, and SHALL use them in listed order. Startup SHALL fail if fewer than one is resolvable and SHALL warn if fewer than three are.

#### Scenario: Tokens used in order
- **WHEN** all tokens have quota
- **THEN** every request uses the first token

#### Scenario: Missing env var
- **WHEN** the second token's environment variable is unset
- **THEN** it is skipped, a warning is logged, and the third token remains available as the next fallback

### Requirement: Failover on quota exhaustion
The runner SHALL switch to the next token when the active token receives HTTP 429 with error code `quota_exhausted` or when its remaining quota is below the planned need, and SHALL retry the failed request once on the new token.

#### Scenario: First token exhausted mid-run
- **WHEN** a request on token 1 returns 429 `quota_exhausted`
- **THEN** the request is retried on token 2 and later requests use token 2

#### Scenario: All tokens exhausted
- **WHEN** every token is exhausted
- **THEN** the runner stops data calls, writes what it already has, and ends the run as `partial` with the reset time

### Requirement: Burst limits back off without failover
The runner SHALL treat 429 `rate_limited` as a per-token burst limit: it SHALL wait for `Retry-After` and retry on the same token, and SHALL NOT fail over or count it as exhaustion.

#### Scenario: Rate limited
- **WHEN** a request returns 429 `rate_limited` with Retry-After 5
- **THEN** the runner waits at least 5 seconds and retries on the same token

### Requirement: Per-token usage tracking
The runner SHALL read each token's quota from the free usage route at start, SHALL count successful charged calls per token and per route during the run, and SHALL record the totals in the run's `data_calls`.

#### Scenario: Usage recorded
- **WHEN** a run finishes
- **THEN** `data_calls` lists calls per token label and per route, without any token value

### Requirement: Quota planning and shedding
Before spending, the runner SHALL estimate planned calls and compare them with the combined remaining quota minus configured reserves. If short, it SHALL shed work in the configured order and never shed open-position bars first.

#### Scenario: Insufficient quota
- **WHEN** planned calls exceed usable quota
- **THEN** optional financials, optional pledge, surveillance, the alert screen and deep-verify count are shed in that order and the shed items are recorded in the run summary

### Requirement: Retry and error semantics
The runner SHALL retry network errors and 5xx responses with the configured backoff, SHALL honour Retry-After on 503, SHALL log 400 and 404 for one symbol and continue, and SHALL never log token values.

#### Scenario: Single-symbol 404
- **WHEN** one symbol returns 404
- **THEN** that symbol is marked as errored and the run continues

#### Scenario: Token in logs
- **WHEN** any request or error is logged
- **THEN** the Authorization header and token value are redacted
