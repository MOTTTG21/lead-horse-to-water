"""Tests for the pure Auth0 helpers.

`is_authenticated` / `get_current_user_sub` take a plain dict (whatever
Starlette's `request.session` looks like at the time), not a real
Request -- kept pure and separate from ASGI machinery so they're
trivially testable. The actual OAuth Authorization Code dance (talking
to a real Auth0 tenant) is exercised manually against a live tenant, not
here -- see the login/callback route tests in test_web_app.py for what
IS covered: the glue code around authlib, using a fake OAuth client.
"""

from __future__ import annotations

from horse_gateway.web.auth import AuthConfig, get_current_user_sub, is_authenticated


def test_auth_config_not_configured_by_default():
    config = AuthConfig()
    assert config.configured is False


def test_auth_config_configured_when_all_three_set():
    config = AuthConfig(
        auth0_domain="dev-example.us.auth0.com",
        auth0_client_id="abc123",
        auth0_client_secret="shh",
    )
    assert config.configured is True


def test_auth_config_not_configured_if_any_one_missing():
    assert AuthConfig(auth0_domain="dev-example.us.auth0.com").configured is False
    assert AuthConfig(auth0_client_id="abc123").configured is False
    assert AuthConfig(auth0_client_secret="shh").configured is False


def test_is_authenticated_false_for_empty_session():
    assert is_authenticated({}) is False


def test_is_authenticated_true_once_user_present():
    assert is_authenticated({"user": {"sub": "auth0|abc123"}}) is True


def test_get_current_user_sub_none_when_not_authenticated():
    assert get_current_user_sub({}) is None


def test_get_current_user_sub_returns_the_sub_claim():
    session = {"user": {"sub": "auth0|abc123", "email": "a@example.com"}}
    assert get_current_user_sub(session) == "auth0|abc123"
