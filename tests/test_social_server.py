"""Tests for the third-party social-posting MCP server.

Session-level caps and external_call auditing are the gateway's job (see
test_gateway.py's social-post-cap tests) -- this module is only
responsible for the external call itself and for surfacing an honest
identifier the gateway logs verbatim.
"""

import pytest

from horse_gateway.social_server import (
    SocialPostFailedError,
    StableSocialServer,
    UnknownToolError,
)


class FakeSocialMediaClient:
    """A fake standing in for a real third-party API client, so tests
    don't depend on network access or real credentials."""

    def __init__(self, fail: bool = False):
        self.published: list[str] = []
        self._fail = fail

    def publish(self, message: str):
        if self._fail:
            raise SocialPostFailedError("simulated third-party outage")
        self.published.append(message)
        from horse_gateway.social_server import SocialPost

        return SocialPost(post_id=f"fake-{len(self.published)}", message=message)


def test_post_to_stable_social_publishes_via_the_client():
    client = FakeSocialMediaClient()
    server = StableSocialServer(client=client)
    result = server.execute("post_to_stable_social", {"message": "hay delivery today!"})
    assert result["post_id"] == "fake-1"
    assert result["message"] == "hay delivery today!"
    assert client.published == ["hay delivery today!"]


def test_post_with_empty_message_is_rejected():
    server = StableSocialServer(client=FakeSocialMediaClient())
    with pytest.raises(SocialPostFailedError):
        server.execute("post_to_stable_social", {"message": ""})


def test_client_failure_propagates_so_the_gateway_can_audit_a_failed_external_call():
    server = StableSocialServer(client=FakeSocialMediaClient(fail=True))
    with pytest.raises(SocialPostFailedError):
        server.execute("post_to_stable_social", {"message": "hello"})


def test_unknown_tool_raises():
    server = StableSocialServer(client=FakeSocialMediaClient())
    with pytest.raises(UnknownToolError):
        server.execute("delete_all_posts", {})


def test_default_client_is_the_mock_when_none_supplied():
    """No real credentials exist yet -- the default client must be safe
    to use out of the box."""
    server = StableSocialServer()
    result = server.execute("post_to_stable_social", {"message": "barn update"})
    assert result["post_id"]
    assert result["message"] == "barn update"
