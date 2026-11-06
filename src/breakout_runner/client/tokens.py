"""Ordered token pool. Token values never leave this module except in the auth header."""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..config.models import Tapetide

logger = logging.getLogger(__name__)


class NoTokensError(Exception):
    """No configured token resolved from the environment."""


class AllTokensExhausted(Exception):
    def __init__(self, reset_at: int | None = None):
        self.reset_at = reset_at
        super().__init__(f"all Tapetide tokens exhausted; reset_at={reset_at}")


@dataclass
class TokenState:
    label: str
    value: str = field(repr=False)
    remaining: int | None = None  # None: not read yet, or an unlimited plan
    reset_at: int | None = None
    exhausted: bool = False
    calls: Counter = field(default_factory=Counter)


class TokenPool:
    def __init__(self, states: list[TokenState]):
        if not states:
            raise NoTokensError("no Tapetide token resolved from the environment")
        self._states = states
        self._index = 0

    @classmethod
    def from_config(cls, cfg: Tapetide, environ: Mapping[str, str] | None = None) -> TokenPool:
        env = os.environ if environ is None else environ
        states = []
        for source in cfg.tokens:
            value = env.get(source.env, "").strip()
            if value:
                states.append(TokenState(label=source.label, value=value))
            else:
                logger.warning("Tapetide token %r: %s is not set", source.label, source.env)
        pool = cls(states)
        if len(states) < cfg.recommended_tokens:
            logger.warning(
                "%d Tapetide token(s) available, %d recommended",
                len(states),
                cfg.recommended_tokens,
            )
        return pool

    @property
    def states(self) -> list[TokenState]:
        return list(self._states)

    def active(self) -> TokenState:
        # Wraps around: a token skipped by ensure_capacity still holds usable quota.
        count = len(self._states)
        for offset in range(count):
            position = (self._index + offset) % count
            if not self._states[position].exhausted:
                self._index = position
                return self._states[position]
        resets = [s.reset_at for s in self._states if s.reset_at is not None]
        raise AllTokensExhausted(min(resets) if resets else None)

    def mark_exhausted(self, state: TokenState, reset_at: int | None = None) -> None:
        state.exhausted = True
        state.remaining = 0
        state.reset_at = reset_at if reset_at is not None else state.reset_at
        logger.warning("Tapetide token %r exhausted; failing over", state.label)

    def mark_invalid(self, state: TokenState) -> None:
        state.exhausted = True
        logger.error("Tapetide token %r was rejected; skipping it", state.label)

    def record_quota(self, state: TokenState, remaining: int | None, reset_at: int | None) -> None:
        if remaining is not None:
            state.remaining = remaining
        if reset_at is not None:
            state.reset_at = reset_at

    def record_call(self, state: TokenState, route: str) -> None:
        state.calls[route] += 1
        if state.remaining is not None:
            state.remaining = max(0, state.remaining - 1)

    def ensure_capacity(self, need: int) -> None:
        """Advance past tokens that cannot cover `need` when a later one can do better."""
        current = self.active()
        if current.remaining is None or current.remaining >= need:
            return
        for position, later in enumerate(self._states):
            if position <= self._index or later.exhausted:
                continue
            if later.remaining is None or later.remaining > current.remaining:
                logger.info(
                    "Tapetide token %r has %d left, need %d; moving to %r",
                    current.label,
                    current.remaining,
                    need,
                    later.label,
                )
                self._index = position
                return

    def combined_remaining(self) -> int | None:
        """Quota left across usable tokens; None means effectively unlimited or unread."""
        total = 0
        for state in self._states:
            if state.exhausted:
                continue
            if state.remaining is None:
                return None
            total += state.remaining
        return total

    def usage_report(self) -> dict[str, dict]:
        """Per-token, per-route counts for runs.data_calls. Labels only, never values."""
        return {
            s.label: {
                "total": sum(s.calls.values()),
                "routes": dict(sorted(s.calls.items())),
                "remaining": s.remaining,
                "exhausted": s.exhausted,
            }
            for s in self._states
        }

    def redact(self, text: str) -> str:
        for state in self._states:
            text = text.replace(state.value, f"<token:{state.label}>")
        return text


class RedactingFilter(logging.Filter):
    """Last line of defence: scrubs token values from any log record."""

    def __init__(self, pool: TokenPool):
        super().__init__()
        self._pool = pool

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._pool.redact(record.getMessage())
        record.args = ()
        return True
