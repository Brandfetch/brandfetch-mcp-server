"""The send_feedback nudges added for PRD-4952: error messages and
docstrings steer clients toward reporting problems."""

import asyncio
import json

import pytest

from src import main
from src.main import _error_message, _tool_error


def test_error_message_nudges_on_data_gaps_and_upstream_failures():
    assert "send_feedback" in _error_message(404, "", "Brand not found.")
    assert "send_feedback" in _error_message(500, "boom", "")
    # An unmapped status still carries the original error detail.
    assert "Brandfetch API error (500): boom" in _error_message(500, "boom", "")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_error_message_skips_nudge_on_caller_side_errors(status):
    assert "send_feedback" not in _error_message(status, "", "")


def test_tool_error_hints_on_eligible_codes():
    for code in ("fetch_failed", "not_found"):
        payload = json.loads(_tool_error(code, "x"))
        assert "send_feedback" in payload["hint"]


@pytest.mark.parametrize(
    "code",
    [
        "invalid_input",
        "hotlink_blocked",
        "asset_too_large",
        # send_feedback's own failure must never nudge toward send_feedback.
        "delivery_failed",
    ],
)
def test_tool_error_has_no_hint_on_excluded_codes(code):
    payload = json.loads(_tool_error(code, "x"))
    assert payload == {"code": code, "message": "x"}


def test_instructions_and_data_quality_tools_mention_send_feedback():
    assert "send_feedback" in (main.mcp.instructions or "")

    tools = {t.name: t for t in asyncio.run(main.mcp.list_tools())}
    for name in ("get_brand", "get_brand_context"):
        assert "send_feedback" in (tools[name].description or ""), name
