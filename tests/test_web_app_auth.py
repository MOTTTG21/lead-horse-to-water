"""Tests for the Auth0 login/callback/logout glue in the web app, using
a fake OAuth client instead of a live Auth0 tenant.

These exercise exactly the code this project owns: that /login calls
authorize_redirect with the right callback URL, that /callback stores
whatever authlib hands back as the session's user and redirects home,
that protected routes 401 without a session and work after one exists,
and that /logout clears the session and builds a plausible Auth0 logout
URL. What they do NOT (and can't, without a live tenant) verify: that
authlib's real OIDC discovery and token exchange against a real Auth0
domain actually succeeds -- that's a one-time manual check once real
AUTH0_DOMAIN / AUTH0_CLIENT_ID / AUTH0_CLIENT_SECRET are in .env.
"""

from __future__ import annotations

from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.llm_metrics import LLMMetricsLog
from horse_gateway.web.app import create_app
from horse_gateway.web.auth import AuthConfig

from test_web_app import ScriptedAnthropicClient


class FakeAuth0Client:
    def __init__(self, userinfo: dict, fail_token_exchange: bool = False):
        self.userinfo = userinfo
        self.authorize_redirect_calls: list[str] = []
        self.authorize_access_token_calls = 0
        self.fail_token_exchange = fail_token_exchange

    async def authorize_redirect(self, request, redirect_uri: str):
        self.authorize_redirect_calls.append(redirect_uri)
        return RedirectResponse(url="https://fake-tenant.us.auth0.com/authorize?redirect_uri=" + redirect_uri)

    async def authorize_access_token(self, request):
        self.authorize_access_token_calls += 1
        if self.fail_token_exchange:
            raise RuntimeError("invalid_grant: authorization code already redeemed")
        return {"userinfo": self.userinfo}


class FakeOAuth:
    def __init__(self, userinfo: dict, fail_token_exchange: bool = False):
        self.auth0 = FakeAuth0Client(userinfo, fail_token_exchange=fail_token_exchange)


def make_authed_test_client(
    userinfo: dict | None = None, allowed_emails: str = "", fail_token_exchange: bool = False
):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit_log = AuditLog(engine)
    llm_metrics_log = LLMMetricsLog(engine)
    fake_oauth = FakeOAuth(
        userinfo or {"sub": "auth0|test-user-1", "email": "a@example.com"},
        fail_token_exchange=fail_token_exchange,
    )
    auth_config = AuthConfig(
        auth0_domain="fake-tenant.us.auth0.com",
        auth0_client_id="fake-client-id",
        auth0_client_secret="fake-client-secret",
        allowed_emails=allowed_emails,
    )
    app = create_app(
        anthropic_client=ScriptedAnthropicClient(),
        audit_log=audit_log,
        llm_metrics_log=llm_metrics_log,
        config=GameConfig(),
        auth_config=auth_config,
        oauth=fake_oauth,
        start_synthetic_traffic=False,
    )
    return TestClient(app), fake_oauth


def test_index_redirects_to_login_when_not_authenticated():
    client, _ = make_authed_test_client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_login_calls_authorize_redirect_with_the_callback_url():
    client, fake_oauth = make_authed_test_client()
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 307
    assert len(fake_oauth.auth0.authorize_redirect_calls) == 1
    assert fake_oauth.auth0.authorize_redirect_calls[0].endswith("/callback")


def test_protected_endpoints_401_without_login():
    client, _ = make_authed_test_client()
    assert client.post("/tool/graze").status_code == 401
    assert client.post("/turn", json={"message": "hi"}).status_code == 401


def test_callback_logs_the_user_in_and_index_stops_redirecting():
    client, fake_oauth = make_authed_test_client()

    callback_response = client.get("/callback?code=fake-code", follow_redirects=False)
    assert callback_response.status_code == 307
    assert callback_response.headers["location"] == "/"
    assert fake_oauth.auth0.authorize_access_token_calls == 1

    index_response = client.get("/")
    assert index_response.status_code == 200

    tool_response = client.post("/tool/graze")
    assert tool_response.status_code == 200
    assert tool_response.json()["decision"] == "allowed"


def test_logout_clears_session_and_builds_auth0_logout_url():
    client, _ = make_authed_test_client()
    client.get("/callback?code=fake-code", follow_redirects=False)
    assert client.get("/").status_code == 200  # logged in

    logout_response = client.get("/logout", follow_redirects=False)
    assert logout_response.status_code == 307
    location = logout_response.headers["location"]
    assert location.startswith("https://fake-tenant.us.auth0.com/v2/logout")
    assert "client_id=fake-client-id" in location
    assert "returnTo=" in location

    # Session cleared -- back to redirecting to /login.
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_unconfigured_auth_has_no_login_routes():
    """Sanity check on the off switch: with no Auth0 env vars set (the
    default), /login shouldn't exist at all -- there's nothing to log
    into, and the app should behave exactly as it did before this
    module existed."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    app = create_app(
        anthropic_client=ScriptedAnthropicClient(),
        audit_log=AuditLog(engine),
        llm_metrics_log=LLMMetricsLog(engine),
        config=GameConfig(),
        start_synthetic_traffic=False,
    )
    client = TestClient(app)
    assert client.get("/login", follow_redirects=False).status_code == 404
    assert client.get("/").status_code == 200
    assert client.post("/tool/graze").status_code == 200


# --- email allowlist ---


def test_callback_rejects_a_non_allowed_email():
    client, _ = make_authed_test_client(
        userinfo={"sub": "auth0|stranger", "email": "stranger@example.com"},
        allowed_emails="owner@example.com",
    )
    response = client.get("/callback?code=fake-code")
    assert response.status_code == 403
    assert "private" in response.text.lower()
    assert '/login' in response.text  # a way back, not a dead end

    # No session was granted -- still bounced to /login.
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_callback_with_an_already_redeemed_code_fails_gracefully():
    """Refreshing the callback page re-submits the same one-time
    authorization code, which Auth0 rejects on reuse -- this must not
    surface as an unhandled 500."""
    client, _ = make_authed_test_client(fail_token_exchange=True)
    response = client.get("/callback?code=already-used")
    assert response.status_code == 400
    assert '/login' in response.text


def test_callback_admits_an_allowed_email():
    client, _ = make_authed_test_client(
        userinfo={"sub": "auth0|owner", "email": "owner@example.com"},
        allowed_emails="owner@example.com",
    )
    response = client.get("/callback?code=fake-code", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_callback_allowlist_is_case_insensitive():
    client, _ = make_authed_test_client(
        userinfo={"sub": "auth0|owner", "email": "Owner@Example.com"},
        allowed_emails="owner@example.com",
    )
    response = client.get("/callback?code=fake-code", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/"
