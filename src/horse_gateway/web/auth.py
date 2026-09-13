"""Real Auth0 identity, swapped in behind the same seam the app was
built with (`get_current_session` in app.py) without touching anything
downstream of it.

Deliberately OFF by default: if AUTH0_DOMAIN / AUTH0_CLIENT_ID /
AUTH0_CLIENT_SECRET aren't all set, `AuthConfig.configured` is False and
the app falls back to today's stub identity (every visitor is `guest`,
no login required) -- so every test written before this module existed
keeps passing unmodified, and local dev doesn't require a live Auth0
tenant.

When configured, this uses the standard Authorization Code flow for a
server-side web app (not an SPA/implicit flow, since this app renders
HTML and owns its own session server-side) via authlib's Starlette
integration, which handles the OAuth `state`/`nonce` CSRF protection
itself using Starlette's SessionMiddleware. This module only owns the
thin glue around that: registering the client, and two pure functions
(`is_authenticated`, `get_current_user_sub`) that read the already-
verified user info back out of the session -- kept pure and separate
from Starlette's Request object so they're trivially unit-testable.

Every real Auth0 login maps to Role.GUEST, full stop -- `stablehand`
stays exclusively synthetic (see synthetic_traffic.py). This was a
deliberate scope decision, not an oversight: the game's premise is that
the player is a visitor, not stable staff.
"""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    auth0_domain: str = ""
    auth0_client_id: str = ""
    auth0_client_secret: str = ""
    session_secret_key: str = "dev-insecure-session-secret-key"

    @property
    def configured(self) -> bool:
        return bool(self.auth0_domain and self.auth0_client_id and self.auth0_client_secret)

    @property
    def logout_return_to_url_param(self) -> str:
        return "returnTo"


def register_oauth(config: AuthConfig) -> OAuth:
    """Builds the authlib OAuth registry. Only meaningful (and only
    called) when config.configured is True."""
    oauth = OAuth()
    oauth.register(
        "auth0",
        client_id=config.auth0_client_id,
        client_secret=config.auth0_client_secret,
        client_kwargs={"scope": "openid profile email"},
        server_metadata_url=f"https://{config.auth0_domain}/.well-known/openid-configuration",
    )
    return oauth


def logout_url(config: AuthConfig, return_to: str) -> str:
    return f"https://{config.auth0_domain}/v2/logout?client_id={config.auth0_client_id}&returnTo={return_to}"


# --- pure helpers over a plain session dict (Starlette's request.session) ---


def is_authenticated(session: dict[str, Any]) -> bool:
    return session.get("user") is not None


def get_current_user_sub(session: dict[str, Any]) -> str | None:
    user = session.get("user")
    if not user:
        return None
    return user.get("sub")
