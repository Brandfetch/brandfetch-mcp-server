"""Quota-exhaustion error clarity added for PRD-5024: 429/403 messages must
say what actually happened and point at the remedy, not at the API key."""

import json

import pytest

from src.main import DASHBOARD_URL, _error_message


QUOTA_BODY = json.dumps(
    {"message": "API key quota exceeded", "quota": 250, "used": 251}
)


def test_429_includes_usage_numbers_when_the_body_carries_them():
    message = _error_message(429, QUOTA_BODY, "")
    assert "API quota exhausted (used 251 of 250 credits)" in message
    assert DASHBOARD_URL in message
    assert "brand_search" in message and "build_logo_urls" in message


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        json.dumps({"message": "API key quota exceeded"}),
        json.dumps({"quota": "250", "used": "251"}),
    ],
)
def test_429_falls_back_to_the_generic_message_on_any_other_body(body):
    message = _error_message(429, body, "")
    assert message.startswith("API quota exhausted:")
    assert DASHBOARD_URL in message


def test_429_makes_no_claim_about_reset_timing():
    # Older plans reset monthly, newer ones have a quota lifetime — the
    # message must stay true if that business logic changes.
    for body in (QUOTA_BODY, ""):
        message = _error_message(429, body, "").lower()
        assert "month" not in message
        assert "reset" not in message


def test_403_does_not_call_the_key_invalid():
    # A plan with zero API credits also answers 403 — blaming the key sends
    # users debugging a perfectly valid one.
    message = _error_message(403, "", "")
    assert "invalid" not in message.lower()
    assert "API credits" in message
    assert DASHBOARD_URL in message
