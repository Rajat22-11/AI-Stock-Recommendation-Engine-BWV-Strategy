"""HTTP transport: retries, burst backoff, and failover on quota exhaustion."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx

from ..config.models import Http
from ..units import SECONDS_PER_MINUTE
from .tokens import TokenPool, TokenState

logger = logging.getLogger(__name__)

RATE_LIMITED = "rate_limited"
QUOTA_EXHAUSTED = "quota_exhausted"
AUTH_FAILURES = {"authentication_required", "invalid_token"}
USAGE_PATH = "/v1/me/usage"


class ApiError(Exception):
    """A non-retryable response, such as a 400 or 404 for one symbol."""

    def __init__(self, route: str, status: int, code: str | None, message: str = ""):
        self.route = route
        self.status = status
        self.code = code
        super().__init__(f"{route}: HTTP {status} {code or ''} {message}".strip())


class ServiceUnavailable(Exception):
    """Retries ran out on network errors or 5xx responses."""


def _error_of(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        err = resp.json().get("error", {})
        return err.get("code"), err.get("message", "")
    except (ValueError, AttributeError):
        return None, ""


def _int_header(resp: httpx.Response, name: str) -> int | None:
    raw = resp.headers.get(name)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


class TapetideClient:
    def __init__(
        self,
        base_url: str,
        cfg: Http,
        pool: TokenPool,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.pool = pool
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self._http = httpx.Client(
            base_url=base_url, timeout=cfg.timeout_seconds, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        gap = SECONDS_PER_MINUTE / self._cfg.burst_per_minute
        if self._last_request is not None:
            wait = gap - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _send(self, state: TokenState, path: str, params: dict | None) -> httpx.Response:
        self._throttle()
        return self._http.get(
            path, params=params, headers={"Authorization": f"Bearer {state.value}"}
        )

    def _backoff(self, attempt: int, retry_after: int | None = None) -> None:
        steps = self._cfg.backoff_seconds
        delay = steps[min(attempt, len(steps) - 1)]
        self._sleep(max(delay, retry_after or 0))

    def request(
        self, route: str, path: str, params: dict | None = None, *, charged: bool = True
    ) -> dict[str, Any]:
        failures = 0
        rate_waits = 0
        while True:
            state = self.pool.active()  # raises AllTokensExhausted when none are left
            try:
                resp = self._send(state, path, params)
            except httpx.TransportError as exc:
                if failures >= self._cfg.retries:
                    raise ServiceUnavailable(f"{route}: {type(exc).__name__}") from None
                self._backoff(failures)
                failures += 1
                continue

            status = HTTPStatus(resp.status_code)
            remaining = _int_header(resp, "X-RateLimit-Remaining")
            reset_at = _int_header(resp, "X-RateLimit-Reset")
            retry_after = _int_header(resp, "Retry-After")

            if status.is_success:
                # Exact count when the server states one; our own decrement otherwise.
                if charged:
                    self.pool.record_call(state, route)
                self.pool.record_quota(state, remaining, reset_at)
                return resp.json()

            code, message = _error_of(resp)
            if status == HTTPStatus.TOO_MANY_REQUESTS:
                if code == QUOTA_EXHAUSTED or (code != RATE_LIMITED and remaining == 0):
                    self.pool.mark_exhausted(state, reset_at)
                    continue
                if rate_waits >= self._cfg.max_rate_limit_waits:
                    raise ApiError(route, resp.status_code, code, "burst limit persisted")
                self._backoff(rate_waits, retry_after)
                rate_waits += 1
                continue
            if code in AUTH_FAILURES or status == HTTPStatus.UNAUTHORIZED:
                self.pool.mark_invalid(state)
                continue
            if status.is_server_error:
                if failures >= self._cfg.retries:
                    raise ServiceUnavailable(f"{route}: HTTP {resp.status_code}")
                self._backoff(failures, retry_after)
                failures += 1
                continue
            raise ApiError(route, resp.status_code, code, message)

    def refresh_usage(self) -> None:
        """Read every token's quota from the free usage route."""
        for state in self.pool.states:
            if state.exhausted:
                continue
            try:
                resp = self._send(state, USAGE_PATH, None)
            except httpx.TransportError:
                logger.warning("usage read failed for token %r", state.label)
                continue
            if HTTPStatus(resp.status_code) == HTTPStatus.UNAUTHORIZED:
                self.pool.mark_invalid(state)
                continue
            if not HTTPStatus(resp.status_code).is_success:
                logger.warning("usage read for %r: HTTP %s", state.label, resp.status_code)
                continue
            data = resp.json()["data"]
            windows = [w for w in (data.get("day"), data.get("month")) if w]
            limits = [w["remaining"] for w in windows if w.get("remaining") is not None]
            if limits:
                self.pool.record_quota(state, min(limits), None)
                if min(limits) <= 0:
                    self.pool.mark_exhausted(state)
