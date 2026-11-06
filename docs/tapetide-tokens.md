# Tapetide tokens and failover

The runner takes an ordered list of tokens from `config/breakout.config.yaml`. Each entry names an environment variable; the value itself never goes in the file.

```yaml
tapetide:
  tokens:
    - {label: primary, env: TAPETIDE_TOKEN}
    - {label: secondary, env: TAPETIDE_TOKEN_2}
    - {label: tertiary, env: TAPETIDE_TOKEN_3}
```

To add a fourth token, add a line here and set the variable. Logs and `runs.data_calls` only ever show the label.

## What happens on each response

| Response | What the runner does |
|---|---|
| Success | Stays on the current token and counts the call by token and route |
| 429 `quota_exhausted` | Marks the token exhausted, retries the same request on the next token, and stays there |
| 429 `rate_limited` | Waits for `Retry-After` and retries on the same token; this is a burst limit, not exhaustion |
| 401 or `invalid_token` | Skips that token for the rest of the run and logs an error |
| 5xx or network error | Retries on the same token with the configured backoff |
| 400 or 404 | Fails only that request, for example one symbol, and the run continues |

If every token is exhausted, the runner stops making data calls and raises with the earliest reset time. Tapetide's daily window resets at midnight IST.

## Before spending quota

At the start of a run the runner reads each token's remaining quota from `/v1/me/usage`, which is free. If the active token cannot cover the planned calls and a later token has more room, it moves on. A token skipped this way stays available as a fallback.

If the combined quota, less `quota.reserve_calls` and `quota.reserve_for_claude`, is too small, the runner sheds work in the order set by `quota.shed_order`. Open-position bars are never shed.

Note that tokens on the same Tapetide account may share one quota. Failover only adds capacity if each token has its own.

## Checking your setup

This validates the config and lists which token variables are set. It makes no network call.

```bash
python -m breakout_runner.cli check-config
```

## What the live API showed (6 Nov 2026)

Measured by reading `/v1/me/usage` after each call.

- The limit is 50 units a day per token on the free plan. REST and MCP share the counter, so REST is not unlimited.
- Two tokens kept separate counters: spending on one left the other at 0.
- Each successful call costs 1 unit, whatever its size. `/v1/me/usage`, `/v1/market/status` and both field catalogues cost 0.
- `ratio_values` on `/v1/screens/fundamental` is not reliable. On the Q1 and Q2 queries, 7 of 10 ratios were null on every row, while the filter itself held on all 8 names checked against bars.
- `/v1/market/surveillance` takes `limit` up to 1000. All 490 rows fit in 1 unit; the default of 100 per page needs 5.
- Symbols with `&` work unencoded in the path (`M&M` returned 200).
