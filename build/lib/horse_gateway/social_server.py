"""Mock MCP tool server for the third-party social-posting integration.

`post_to_stable_social` is the one action in this whole system that
leaves the system boundary: a real, external, irreversible, visible-
outside-the-system call. Session-level caps and `external_call` auditing
are the gateway's job (see Gateway._guardrail_denial_reason and
Gateway._write_audit_row) -- this module is only responsible for the
external call itself, via an injectable client, so a real third-party API
integration can be swapped in later without touching the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class UnknownToolError(Exception):
    pass


class SocialPostFailedError(Exception):
    pass


@dataclass(frozen=True)
class SocialPost:
    post_id: str
    message: str


class SocialMediaClient(Protocol):
    def publish(self, message: str) -> SocialPost: ...


class MockSocialMediaClient:
    """Stand-in for a real third-party API client (e.g. Mastodon,
    Bluesky). No real credentials exist yet, so this is the default."""

    def __init__(self):
        self._next_id = 1

    def publish(self, message: str) -> SocialPost:
        post = SocialPost(post_id=f"mock-post-{self._next_id}", message=message)
        self._next_id += 1
        return post


class StableSocialServer:
    """Validates the outgoing message before it ever reaches a client --
    a real third-party API might also reject an empty post, but this
    server can't assume every client it's ever handed will bother to
    check, so it enforces the rule itself."""

    def __init__(self, client: SocialMediaClient | None = None):
        self._client = client or MockSocialMediaClient()

    def execute(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "post_to_stable_social":
            raise UnknownToolError(tool)
        message = params.get("message", "")
        if not message or not message.strip():
            raise SocialPostFailedError("message must be non-empty")
        post = self._client.publish(message)
        return {"post_id": post.post_id, "message": post.message}
