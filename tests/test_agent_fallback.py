"""
Tests for the agent's graceful-degradation (fallback) behaviour.

A reviewer will try to *break* the app — pull the network, use a bad key, trigger a
rate limit. These tests confirm that when the underlying model call fails, ``run_agent``
never raises: it returns a calm, plain-language message instead of letting a traceback
reach the UI. The Anthropic client is monkeypatched to raise each failure mode, so the
tests run offline with no real API calls.

Run with:  ``pytest``  (or click "Run Python File" in VS Code).
"""

import os
import sys
from typing import Callable

import httpx
import pytest

# Allow running this file directly as well as via pytest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

import agent

# A dummy HTTP request/response pair, needed to construct the SDK's exception types.
_DUMMY_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _raise(exc: BaseException) -> Callable[..., None]:
    """Return a stand-in for ``messages.create`` that always raises ``exc``."""
    def _fail(*args: object, **kwargs: object) -> None:
        raise exc
    return _fail


def _patch_create(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make the agent's Anthropic client raise ``exc`` on its next ``messages.create`` call."""
    monkeypatch.setattr(agent._client.messages, "create", _raise(exc))


def test_network_failure_returns_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection error (e.g. the reviewer pulls the network) yields a calm
    'network issue' message rather than raising."""
    _patch_create(monkeypatch, anthropic.APIConnectionError(request=_DUMMY_REQUEST))
    answer: str = agent.run_agent("Compare LA and Santa Ana congestion.")
    assert isinstance(answer, str) and answer
    assert "network" in answer.lower()


def test_rate_limit_returns_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate-limit error asks the user to wait and retry, without raising."""
    rate_limit = anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=_DUMMY_REQUEST), body=None
    )
    _patch_create(monkeypatch, rate_limit)
    answer: str = agent.run_agent("Rank New England airports.")
    assert isinstance(answer, str) and answer
    assert "try again" in answer.lower()


def test_auth_failure_returns_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad/missing API key surfaces as a configuration message, not a crash."""
    auth_error = anthropic.AuthenticationError(
        "bad key", response=httpx.Response(401, request=_DUMMY_REQUEST), body=None
    )
    _patch_create(monkeypatch, auth_error)
    answer: str = agent.run_agent("What is the unmet demand at SFO?")
    assert isinstance(answer, str) and answer
    assert "configuration" in answer.lower()


def test_unexpected_error_returns_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other unexpected error is also caught and reported gently."""
    _patch_create(monkeypatch, RuntimeError("boom"))
    answer: str = agent.run_agent("Anything at all.")
    assert isinstance(answer, str) and answer
    assert "unexpected" in answer.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
