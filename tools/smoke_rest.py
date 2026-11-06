"""T-01: call each REST route the runner uses for one symbol and record the field names.

Needs a real token in TAPETIDE_TOKEN and spends about 10 quota units.
Run: python tools/smoke_rest.py [SYMBOL]. Writes fixtures/rest_smoke.json.
Values are not stored, only field names and types, so the file is safe to commit.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from breakout_runner.client import TapetideClient, TokenPool
from breakout_runner.client.http import ApiError
from breakout_runner.config.loader import load_config
from breakout_runner.intake.screens import build_query

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "fixtures" / "rest_smoke.json"


def shape(value, depth=0):
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())} if depth < 4 else "object"
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def main(argv: list[str]) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    symbol = argv[0] if argv else "ACMESOLAR"
    config = load_config(ROOT / "config" / "breakout.config.yaml")[0]
    pool = TokenPool.from_config(config.tapetide)
    client = TapetideClient(config.tapetide.base_url, config.http, pool)
    today = dt.date.today()
    year_start = today.replace(month=1, day=1).isoformat()
    routes = {
        "usage": ("/v1/me/usage", {}, False),
        "market_status": ("/v1/market/status", {}, False),
        "screen_fields": ("/v1/screens/fundamental/fields", {}, False),
        "holidays": ("/v1/market/holidays", {"from": year_start}, True),
        "price": (
            f"/v1/stocks/{symbol}/price",
            {"days": config.data.lookback_calendar_days, "interval": "daily"},
            True,
        ),
        "screen_q1": (
            "/v1/screens/fundamental",
            {
                "query": build_query(config.screens.trigger_q1, config.screens.common),
                "sort": config.screens.sort,
                "order": config.screens.order,
                "limit": config.screens.page_limit,
            },
            True,
        ),
        "heatmap": (f"/v1/market/heatmap/{config.rs.universe_index}", {"limit": 500}, True),
        "index_levels": (
            f"/v1/indices/{config.rs.universe_index}/levels",
            {"from": year_start},
            True,
        ),
        "surveillance": (
            "/v1/market/surveillance",
            {"limit": config.surveillance.page_limit},
            True,
        ),
        "corporate_actions": (
            "/v1/market/corporate-actions",
            {"limit": config.corporate_actions.page_limit},
            True,
        ),
        "quotes_live": ("/v1/quotes/live", {"symbols": symbol}, True),
        "fundamentals": (
            f"/v1/stocks/{symbol}/fundamentals",
            {"period_type": "quarter", "metrics": "revenue,eps,net_profit,other_income"},
            True,
        ),
        "signals": ("/v1/market/signals", {"signal": "near_52w_high", "limit": 5}, True),
        "pledge": (f"/v1/stocks/{symbol}/promoter-pledge", {}, True),
        "shareholding": (f"/v1/stocks/{symbol}/shareholding", {}, True),
    }
    results = {}
    failures = 0
    for name, (path, params, charged) in routes.items():
        try:
            body = client.request(name, path, params, charged=charged)
            results[name] = {"ok": True, "shape": shape(body)}
            print(f"ok   {name}")
        except ApiError as exc:
            failures += 1
            results[name] = {"ok": False, "status": exc.status, "code": exc.code}
            print(f"FAIL {name}: {exc.status} {exc.code}")
    payload = {"symbol": symbol, "date": today.isoformat(), "routes": results}
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
    print(f"wrote {OUTPUT}; usage {json.dumps(pool.usage_report())}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
