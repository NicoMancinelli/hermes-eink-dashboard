from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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
        max_workers: int = 4,
    ) -> None:
        self._lock = threading.Lock()
        self._allowed: set[str] = set(allowlist) if allowlist else set()
        self._default_rate_limit = float(rate_limit_seconds)
        self._action_rate_limits: dict[str, float] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._nonces: dict[str, float] = {}
        self._last_executed: dict[str, float] = {}
        # Handler invocations are dispatched to a thread pool so a slow
        # workflow (up to its timeout) does not block the asyncio event loop.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="action-handler",
        )
        # Track in-flight futures so tests can synchronize via wait_for_pending().
        self._pending: set[Future] = set()

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
            matched_key = action if action in self._allowed else None
            if matched_key is None:
                for allowed_action in self._allowed:
                    if action.startswith(allowed_action + ".") or action.startswith(allowed_action + ":"):
                        matched_key = allowed_action
                        break

            if matched_key is None:
                LOGGER.warning("action=%s result=forbidden", action)
                raise UnknownActionError(f"Action '{action}' is not in the allowlist")

            # 4. Per-action rate limit check
            limit = self._action_rate_limits.get(matched_key, self._default_rate_limit)
            last = self._last_executed.get(matched_key)
            if last is not None and (current_time - last) < limit:
                LOGGER.warning("action=%s result=rate_limited last=%f now=%f", action, last, current_time)
                raise RateLimitExceededError(f"Rate limit exceeded for action '{action}'")

            # Record state
            if nonce:
                self._nonces[nonce] = current_time
            self._last_executed[matched_key] = current_time
            handler = self._handlers.get(matched_key)

        # Execute handler outside the lock and inside the thread pool so a
        # slow handler does not block the dispatch caller or other concurrent
        # dispatches. Errors are logged but never propagated to the caller —
        # the dispatch has already been validated and accepted.
        if handler is not None:
            future: Future = self._executor.submit(
                handler, tile_id=tile_id, action=action, nonce=nonce, ts=ts_float
            )
            with self._lock:
                self._pending.add(future)
            future.add_done_callback(self._drop_pending)

        LOGGER.info("action=%s result=ok tile_id=%s nonce=%s", action, tile_id, nonce)
        return {
            "tile_id": tile_id,
            "action": action,
            "nonce": nonce,
            "ts": ts_float,
        }

    @staticmethod
    def _log_handler_result(future: Future) -> None:
        try:
            future.result()
        except Exception:
            LOGGER.exception("action handler raised after dispatch")

    def _drop_pending(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)
        self._log_handler_result(future)

    def wait_for_pending(self, timeout: float = 30.0) -> None:
        """Block until all currently-queued handler futures have completed.

        Tests use this to synchronize with async handler execution.
        Production code should not call this — handlers are meant to run in
        the background without blocking the request loop.
        """
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                pending = list(self._pending)
            if not pending:
                return
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return
            # Wait on the first pending future to avoid spinning.
            try:
                pending[0].result(timeout=remaining)
            except Exception:
                pass
