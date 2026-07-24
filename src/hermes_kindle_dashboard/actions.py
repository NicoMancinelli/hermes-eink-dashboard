from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

LOGGER = logging.getLogger("hermes-kindle-dashboard.actions")


class ActionError(Exception):
    """Base exception for action registry errors."""


class UnknownActionError(ActionError):
    """Raised when an action is not in the allowlist."""


class InvalidTimestampError(ActionError):
    """Raised when a request timestamp falls outside the +/-30s window."""


class InvalidNonceError(ActionError):
    """Raised when a nonce has already been processed within the 60s TTL."""


class RateLimitExceededError(ActionError):
    """Raised when action dispatch exceeds per-action rate limits."""


class ActionRegistry:
    """Thread-safe registry for host-side action dispatch with rate limits and nonce dedup."""

    def __init__(
        self,
        allowlist: Iterable[str] | None = None,
        rate_limit_seconds: float = 1.0,
    ) -> None:
        self._lock = threading.Lock()
        self._allowed: set[str] = set(allowlist) if allowlist else set()
        self._default_rate_limit = float(rate_limit_seconds)
        self._action_rate_limits: dict[str, float] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._nonces: dict[str, float] = {}
        self._last_executed: dict[str, float] = {}

    def register(
        self,
        action: str,
        handler: Callable[..., Any] | None = None,
        rate_limit: float | None = None,
    ) -> None:
        with self._lock:
            self._allowed.add(action)
            if handler is not None:
                self._handlers[action] = handler
            if rate_limit is not None:
                self._action_rate_limits[action] = float(rate_limit)

    def dispatch(
        self,
        action: str,
        tile_id: str = "",
        nonce: str = "",
        ts: float | int = 0,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = float(now) if now is not None else time.time()
        ts_float = float(ts)

        # 1. Timestamp window check (+/- 30s)
        if abs(current_time - ts_float) > 30.0:
            LOGGER.warning("action=%s result=invalid_timestamp ts=%f now=%f", action, ts_float, current_time)
            raise InvalidTimestampError("Timestamp is outside the allowed +-30s window")

        with self._lock:
            # 2. Nonce TTL cleanup (60s)
            expired = [n for n, record_time in self._nonces.items() if current_time - record_time > 60.0]
            for n in expired:
                del self._nonces[n]

            # Nonce duplicate check
            if nonce and nonce in self._nonces and (current_time - self._nonces[nonce]) <= 60.0:
                LOGGER.warning("action=%s result=duplicate_nonce nonce=%s", action, nonce)
                raise InvalidNonceError("Nonce has already been used within the 60s TTL")

            # 3. Allowlist check
            if action not in self._allowed:
                LOGGER.warning("action=%s result=forbidden", action)
                raise UnknownActionError(f"Action '{action}' is not in the allowlist")

            # 4. Per-action rate limit check
            limit = self._action_rate_limits.get(action, self._default_rate_limit)
            last = self._last_executed.get(action)
            if last is not None and (current_time - last) < limit:
                LOGGER.warning("action=%s result=rate_limited last=%f now=%f", action, last, current_time)
                raise RateLimitExceededError(f"Rate limit exceeded for action '{action}'")

            # Record state
            if nonce:
                self._nonces[nonce] = current_time
            self._last_executed[action] = current_time
            handler = self._handlers.get(action)

        # Execute handler outside lock if present
        if handler is not None:
            handler(tile_id=tile_id, action=action, nonce=nonce, ts=ts_float)

        LOGGER.info("action=%s result=ok tile_id=%s nonce=%s", action, tile_id, nonce)
        return {
            "tile_id": tile_id,
            "action": action,
            "nonce": nonce,
            "ts": ts_float,
        }
