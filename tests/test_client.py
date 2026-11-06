from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import httpx
import pytest

from breakout_runner.client import (
    AllTokensExhausted,
    ApiError,
    NoTokensError,
    ServiceUnavailable,
    TapetideApi,
    TapetideClient,
    TokenPool,
)
from breakout_runner.client.planner import (
    ScanEstimate,
    plan_midweek,
    plan_scan,
    usable_quota,
)
from breakout_runner.client.tokens import RedactingFilter

ENV = {"TAPETIDE_TOKEN": "tok-one", "TAPETIDE_TOKEN_2": "tok-two", "TAPETIDE_TOKEN_3": "tok-three"}
SPEC = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "tapetide-openapi.json").read_text("utf-8")
)


def ok(payload=None, **headers):
    return httpx.Response(200, json=payload or {"data": [], "meta": {}}, headers=headers)


def error(status, code, **headers):
    return httpx.Response(status, json={"error": {"code": code, "message": code}}, headers=headers)


def make(cfg, handler, env=ENV):
    sleeps = []
    pool = TokenPool.from_config(cfg.tapetide, env)
    client = TapetideClient(
        "https://t.example",
        cfg.http,
        pool,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        clock=lambda: 1000.0,
    )
    return client, pool, sleeps


def token_of(request):
    return request.headers["Authorization"].removeprefix("Bearer ")


def test_tokens_used_in_order_while_quota_lasts(cfg):
    seen = []

    def handler(request):
        seen.append(token_of(request))
        return ok()

    client, _, _ = make(cfg, handler)
    for _ in range(3):
        client.request("r", "/x")
    assert seen == ["tok-one"] * 3


def test_missing_env_skips_to_next_and_warns(cfg, caplog):
    seen = []

    def handler(request):
        seen.append(token_of(request))
        return error(429, "quota_exhausted") if token_of(request) == "tok-one" else ok()

    env = {"TAPETIDE_TOKEN": "tok-one", "TAPETIDE_TOKEN_3": "tok-three"}
    with caplog.at_level(logging.WARNING):
        client, _, _ = make(cfg, handler, env)
        client.request("r", "/x")
    assert seen == ["tok-one", "tok-three"]
    assert "TAPETIDE_TOKEN_2" in caplog.text
    assert "recommended" in caplog.text


def test_no_token_fails_startup(cfg):
    with pytest.raises(NoTokensError):
        TokenPool.from_config(cfg.tapetide, {})


def test_failover_on_quota_exhausted_sticks(cfg):
    seen = []

    def handler(request):
        seen.append(token_of(request))
        if token_of(request) == "tok-one":
            return error(429, "quota_exhausted", **{"X-RateLimit-Remaining": "0"})
        return ok()

    client, _, _ = make(cfg, handler)
    client.request("r", "/x")
    client.request("r", "/x")
    assert seen == ["tok-one", "tok-two", "tok-two"]


def test_all_exhausted_reports_reset(cfg):
    def handler(request):
        return error(429, "quota_exhausted", **{"X-RateLimit-Reset": "1800000000"})

    client, _, _ = make(cfg, handler)
    with pytest.raises(AllTokensExhausted) as exc:
        client.request("r", "/x")
    assert exc.value.reset_at == 1800000000


def test_rate_limited_backs_off_without_failover(cfg):
    seen = []
    replies = [error(429, "rate_limited", **{"Retry-After": "5"}), ok()]

    def handler(request):
        seen.append(token_of(request))
        return replies.pop(0)

    client, pool, sleeps = make(cfg, handler)
    client.request("r", "/x")
    assert seen == ["tok-one", "tok-one"]
    assert max(sleeps) >= 5
    assert pool.states[0].exhausted is False


def test_rate_limit_that_never_clears_raises(cfg):
    client, _, _ = make(cfg, lambda r: error(429, "rate_limited", **{"Retry-After": "1"}))
    with pytest.raises(ApiError):
        client.request("r", "/x")


def test_rejected_token_is_skipped(cfg):
    seen = []

    def handler(request):
        seen.append(token_of(request))
        return error(401, "invalid_token") if token_of(request) == "tok-one" else ok()

    client, _, _ = make(cfg, handler)
    client.request("r", "/x")
    assert seen == ["tok-one", "tok-two"]


def test_503_retries_with_retry_after(cfg):
    replies = [error(503, "service_unavailable", **{"Retry-After": "7"}), ok()]
    client, pool, sleeps = make(cfg, lambda r: replies.pop(0))
    client.request("r", "/x")
    assert 7 in sleeps
    assert pool.states[0].calls["r"] == 1


def test_network_errors_retry_then_give_up(cfg):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    client, _, _ = make(cfg, handler)
    with pytest.raises(ServiceUnavailable):
        client.request("r", "/x")
    assert len(calls) == cfg.http.retries + 1


def test_404_is_not_retried_or_charged(cfg):
    calls = []

    def handler(request):
        calls.append(1)
        return error(404, "not_found")

    client, pool, _ = make(cfg, handler)
    with pytest.raises(ApiError) as exc:
        client.request("price", "/x")
    assert exc.value.status == 404 and len(calls) == 1
    assert pool.states[0].calls["price"] == 0


def test_usage_tracking_by_label_and_route_without_values(cfg):
    client, pool, _ = make(cfg, lambda r: ok())
    client.request("price", "/x")
    client.request("price", "/x")
    client.request("heatmap", "/y")
    client.request("market_status", "/z", charged=False)
    report = pool.usage_report()
    assert report["primary"]["routes"] == {"heatmap": 1, "price": 2}
    assert report["primary"]["total"] == 3
    assert "tok-one" not in json.dumps(report)


def test_remaining_from_headers_is_authoritative(cfg):
    client, pool, _ = make(cfg, lambda r: ok(**{"X-RateLimit-Remaining": "42"}))
    client.request("r", "/x")
    assert pool.states[0].remaining == 42


def test_refresh_usage_reads_each_token_at_no_charge(cfg):
    left = {"tok-one": 3, "tok-two": 50, "tok-three": 0}

    def handler(request):
        remaining = left[token_of(request)]
        return ok(
            {
                "data": {
                    "plan": "free",
                    "day": {"remaining": remaining},
                    "month": {"remaining": None},
                },
                "meta": {},
            }
        )

    client, pool, _ = make(cfg, handler)
    client.refresh_usage()
    states = pool.states
    assert [s.remaining for s in states] == [3, 50, 0]
    assert states[2].exhausted and pool.combined_remaining() == 53
    assert sum(s.calls.total() for s in states) == 0


def test_ensure_capacity_moves_to_a_token_with_more_room(cfg):
    client, pool, _ = make(cfg, lambda r: ok())
    pool.states[0].remaining = 3
    pool.states[1].remaining = 50
    pool.ensure_capacity(10)
    assert pool.active().label == "secondary"


def test_token_skipped_for_capacity_remains_a_fallback(cfg):
    seen = []

    def handler(request):
        seen.append(token_of(request))
        return error(429, "quota_exhausted") if token_of(request) != "tok-one" else ok()

    client, pool, _ = make(cfg, handler)
    pool.states[0].remaining = 3
    pool.states[1].remaining = 50
    pool.ensure_capacity(10)
    client.request("r", "/x")
    assert seen == ["tok-two", "tok-three", "tok-one"]
    assert pool.combined_remaining() == 2


def test_ensure_capacity_stays_when_no_later_token_is_better(cfg):
    client, pool, _ = make(cfg, lambda r: ok())
    for state, left in zip(pool.states, [20, 5, 5], strict=True):
        state.remaining = left
    pool.ensure_capacity(30)
    assert pool.active().label == "primary"


def test_token_values_never_reach_logs(cfg, caplog):
    client, pool, _ = make(cfg, lambda r: error(429, "quota_exhausted"))
    logger = logging.getLogger("redact-test")
    logger.addFilter(RedactingFilter(pool))
    with caplog.at_level(logging.DEBUG):
        logger.warning("leaked %s", "tok-two")
        with pytest.raises(AllTokensExhausted):
            client.request("r", "/x")
    assert "<token:secondary>" in caplog.text
    for value in ENV.values():
        assert value not in caplog.text
    assert "tok-one" not in repr(pool.states[0])


def estimate(**over):
    base = dict(
        q1_pages=1,
        q2_pages=2,
        q2_candidates=20,
        q1_candidates=20,
        open_positions=4,
        deep_verify=8,
        surveillance=1,
        heatmap=1,
        holidays=1,
        pledge_names=2,
    )
    return ScanEstimate(**(base | over))


def test_plan_fits_without_shedding(cfg):
    plan = plan_scan(estimate(), 100, cfg.quota)
    assert plan.shed == [] and plan.short is False


def test_shedding_follows_the_configured_order(cfg):
    full = plan_scan(estimate(), None, cfg.quota).total
    assert plan_scan(estimate(), full - 1, cfg.quota).shed == ["financials"]
    assert plan_scan(estimate(), full - 9, cfg.quota).shed == ["financials", "pledge"]
    assert plan_scan(estimate(), full - 11, cfg.quota).shed == [
        "financials",
        "pledge",
        "surveillance",
    ]
    assert plan_scan(estimate(), full - 15, cfg.quota).shed == [
        "financials",
        "pledge",
        "surveillance",
        "alert_screen",
    ]


def test_open_position_bars_are_never_shed(cfg):
    plan = plan_scan(estimate(), 0, cfg.quota)
    assert plan.items["open_position_bars"] == 4
    assert plan.short is True
    assert "open_position_bars" not in plan.shed


def test_usable_quota_subtracts_reserves(cfg):
    assert usable_quota(50, cfg.quota) == 50 - 5 - 8
    assert usable_quota(5, cfg.quota) == 0
    assert usable_quota(None, cfg.quota) is None


def test_midweek_sheds_new_high_first_and_keeps_positions(cfg):
    plan = plan_midweek(25, 4, 1, cfg.quota, None)
    assert plan.shed[0] == "new_high"
    assert plan.items["open_position_bars"] == 4
    assert plan.total <= cfg.quota.midweek_budget_calls


def _example(path):
    content = SPEC["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
    assert "example" in content, path
    return content["example"]


def _routed(cfg, routes):
    def handler(request):
        return ok(_example(routes[request.url.path]))

    client, _, _ = make(cfg, handler)
    return TapetideApi(client)


def test_contract_market_status(cfg):
    api = _routed(cfg, {"/v1/market/status": "/v1/market/status"})
    status = api.market_status()
    assert status.is_trading_day is True and status.session["type"] == "regular"


def test_contract_price(cfg):
    path = "/v1/stocks/{symbol}/price"
    api = _routed(cfg, {"/v1/stocks/X/price": path})
    page = api.price("X", 30)
    assert page.bars and page.bars[0].raw_close is not None
    assert page.next_cursor and page.last_stored_date


@pytest.mark.parametrize(
    "path, call",
    [
        ("/v1/market/holidays", lambda a: a.holidays(dt.date(2026, 1, 1), dt.date(2026, 12, 31))),
        (
            "/v1/screens/fundamental",
            lambda a: a.screen_fundamental("q", "s", "desc", 50).rows,
        ),
        ("/v1/market/surveillance", lambda a: a.surveillance(1000)),
        ("/v1/market/signals", lambda a: a.market_signals("near_52w_high", 500, 50)),
    ],
)
def test_contract_list_routes(cfg, path, call):
    example = _example(path)
    example["meta"].pop("next_cursor", None)
    client, _, _ = make(cfg, lambda r: ok(example))
    rows = call(TapetideApi(client))
    assert rows, path


def test_contract_heatmap_and_fundamentals(cfg):
    heat = _example("/v1/market/heatmap/{index_slug}")
    heat["meta"].pop("next_cursor", None)
    client, _, _ = make(cfg, lambda r: ok(heat))
    assert TapetideApi(client).heatmap("nifty-500", 500)
    funda = _example("/v1/stocks/{symbol}/fundamentals")
    funda["meta"].pop("next_cursor", None)
    client, _, _ = make(cfg, lambda r: ok(funda))
    assert TapetideApi(client).fundamentals("X", ["eps"], "quarter")


def test_walk_follows_cursors_past_short_pages(cfg):
    pages = [
        {"data": [{"a": 1}], "meta": {"next_cursor": "c1"}},
        {"data": [{"a": 2}], "meta": {"next_cursor": None}},
    ]
    seen = []

    def handler(request):
        seen.append(request.url.params.get("cursor"))
        return ok(pages.pop(0))

    client, _, _ = make(cfg, handler)
    rows = TapetideApi(client)._walk("r", "/x", {"limit": 50})
    assert rows == [{"a": 1}, {"a": 2}] and seen == [None, "c1"]


def test_contract_shareholding(cfg):
    example = _example("/v1/stocks/{symbol}/shareholding")
    client, _, _ = make(cfg, lambda r: ok(example))
    table = TapetideApi(client).shareholding("X")
    assert "Promoters" in table and "FIIs" in table
