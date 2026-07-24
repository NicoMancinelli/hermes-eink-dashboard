import time
import pytest

from hermes_kindle_dashboard.actions import (
    ActionError,
    ActionRegistry,
    InvalidNonceError,
    InvalidTimestampError,
    RateLimitExceededError,
    UnknownActionError,
)


def test_action_registry_allowlist() -> None:
    registry = ActionRegistry(allowlist=["workflow.briefing"])

    # Disallowing unknown action
    with pytest.raises(UnknownActionError):
        registry.dispatch("workflow.unknown", tile_id="t1", nonce="n1", ts=time.time())

    # Allowed action succeeds
    now = time.time()
    res = registry.dispatch("workflow.briefing", tile_id="t1", nonce="n1", ts=now)
    assert res["action"] == "workflow.briefing"


def test_action_registry_timestamp_window() -> None:
    registry = ActionRegistry(allowlist=["refresh"], rate_limit_seconds=0)
    now = 1000.0

    # Valid within +-30s window
    registry.dispatch("refresh", tile_id="t1", nonce="n1", ts=1000.0, now=now)
    registry.dispatch("refresh", tile_id="t1", nonce="n2", ts=1025.0, now=now)
    registry.dispatch("refresh", tile_id="t1", nonce="n3", ts=975.0, now=now)

    # Outside window
    with pytest.raises(InvalidTimestampError):
        registry.dispatch("refresh", tile_id="t1", nonce="n4", ts=1031.0, now=now)

    with pytest.raises(InvalidTimestampError):
        registry.dispatch("refresh", tile_id="t1", nonce="n5", ts=969.0, now=now)



def test_action_registry_nonce_dedup() -> None:
    registry = ActionRegistry(allowlist=["refresh"])
    now = 1000.0

    registry.dispatch("refresh", tile_id="t1", nonce="nonce-123", ts=now, now=now)

    # Reusing same nonce within 60s raises InvalidNonceError
    with pytest.raises(InvalidNonceError):
        registry.dispatch("refresh", tile_id="t1", nonce="nonce-123", ts=now + 2.0, now=now + 2.0)

    # After 60s expiry, same nonce can be accepted again if cleaned up
    registry.dispatch("refresh", tile_id="t1", nonce="nonce-123", ts=now + 65.0, now=now + 65.0)


def test_action_registry_rate_limit() -> None:
    registry = ActionRegistry(allowlist=["refresh"], rate_limit_seconds=1.0)
    now = 1000.0

    registry.dispatch("refresh", tile_id="t1", nonce="n1", ts=now, now=now)

    # Rapid second call within 1.0s raises RateLimitExceededError
    with pytest.raises(RateLimitExceededError):
        registry.dispatch("refresh", tile_id="t1", nonce="n2", ts=now + 0.5, now=now + 0.5)

    # After 1.0s delay, succeeds
    registry.dispatch("refresh", tile_id="t1", nonce="n3", ts=now + 1.1, now=now + 1.1)


def test_action_error_hierarchy() -> None:
    assert issubclass(UnknownActionError, ActionError)
    assert issubclass(InvalidTimestampError, ActionError)
    assert issubclass(InvalidNonceError, ActionError)
    assert issubclass(RateLimitExceededError, ActionError)
