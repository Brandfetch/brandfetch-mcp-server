import asyncio
import json

import pytest

from src import main
from src.utils import events

WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/fake"


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; records posts on the class."""

    posts: list[tuple[str, dict]] = []
    status_code = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        type(self).posts.append((url, json))
        return _FakeResponse(type(self).status_code)


@pytest.fixture
def slack(monkeypatch):
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("SLACK_FEEDBACK_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("STAGE", "unit-test")
    return _FakeAsyncClient


@pytest.fixture
def published(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        events, "publish", lambda name, urn, payload: calls.append((name, urn, payload))
    )
    return calls


def _send(**kwargs):
    return asyncio.run(main.send_feedback(**kwargs))


def test_send_feedback_posts_to_slack_and_acks(slack, published):
    result = json.loads(
        _send(
            message="get_brand returned a stale logo",
            category="data-quality",
            tool_name="get_brand",
        )
    )

    assert result["status"] == "received"
    assert "feedback" in result["message"]

    assert len(slack.posts) == 1
    url, payload = slack.posts[0]
    assert url == WEBHOOK_URL
    # The stage leads the message so one shared webhook can serve every
    # environment.
    assert payload["text"].startswith("[unit-test]")
    assert payload["blocks"][0]["elements"][0]["text"] == "[unit-test]"
    assert payload["blocks"][2]["text"]["text"] == "get_brand returned a stale logo"
    context_text = payload["blocks"][3]["elements"][0]["text"]
    assert "data-quality" in context_text
    assert "get_brand" in context_text

    assert len(published) == 1
    name, _urn, event_payload = published[0]
    assert name == "mcp.feedback.submitted"
    assert event_payload["category"] == "data-quality"
    assert event_payload["toolName"] == "get_brand"
    assert event_payload["delivered"] is True


def test_send_feedback_rejects_empty_message(slack, published):
    with pytest.raises(ValueError) as exc_info:
        _send(message="   ")

    assert json.loads(str(exc_info.value))["code"] == "invalid_input"
    assert slack.posts == []
    assert published == []


def test_send_feedback_without_webhook_still_acks(monkeypatch, slack, published):
    monkeypatch.delenv("SLACK_FEEDBACK_WEBHOOK_URL")

    result = json.loads(_send(message="missing font data for nike.com"))

    assert result["status"] == "received"
    assert slack.posts == []
    assert len(published) == 1
    assert published[0][2]["delivered"] is False


def test_send_feedback_raises_when_slack_delivery_fails(slack, published):
    slack.status_code = 500

    with pytest.raises(ValueError) as exc_info:
        _send(message="some feedback")

    assert json.loads(str(exc_info.value))["code"] == "delivery_failed"
    # The event trail still records the attempt.
    assert len(published) == 1
    assert published[0][2]["delivered"] is False


def test_send_feedback_truncates_long_messages(slack, published):
    result = json.loads(_send(message="x" * 5000))

    assert result["status"] == "received"
    _url, payload = slack.posts[0]
    posted_text = payload["blocks"][2]["text"]["text"]
    assert len(posted_text) <= main.FEEDBACK_MAX_CHARS + len("… [truncated]")
    assert posted_text.endswith("… [truncated]")
    assert published[0][2]["truncated"] is True


def test_send_feedback_escapes_slack_control_characters(slack, published):
    _send(message="tags like <script> & <b> broke")

    _url, payload = slack.posts[0]
    assert (
        payload["blocks"][2]["text"]["text"]
        == "tags like &lt;script&gt; &amp; &lt;b&gt; broke"
    )
