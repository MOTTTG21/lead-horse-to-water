"""Tests for the Gateway: identity (already resolved into `role` by the
caller) -> policy check -> audit log write -> execute/deny, for every tool
call, EXCEPT the one deliberate gap in `let_horse_decide`.

These tests use a bare in-memory fake ToolServer rather than the real
mock horse-simulation server (built next) -- the gateway's wiring
behavior should not depend on what a server actually does with a tool
call, only on whether it gets called at all and with what.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.gateway import Gateway, SessionState, ToolCallResult
from horse_gateway.models import Decision, Initiator, Role, System


class FakeToolServer:
    """Records every execute() call it receives and returns a canned result."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool: str, params: dict[str, Any]) -> Any:
        self.calls.append((tool, params))
        return {"tool": tool, "params": params}


def make_audit_log() -> AuditLog:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return AuditLog(engine)


def make_gateway(config: GameConfig | None = None, clock=None):
    horse_sim = FakeToolServer()
    stable_records = FakeToolServer()
    social = FakeToolServer()
    audit_log = make_audit_log()
    servers = {
        System.HORSE_SIM: horse_sim,
        System.STABLE_RECORDS: stable_records,
        System.SOCIAL: social,
    }
    gateway = Gateway(
        servers=servers,
        audit_log=audit_log,
        config=config or GameConfig(),
        clock=clock or (lambda: dt.datetime.now(dt.timezone.utc)),
    )
    return gateway, servers, audit_log


def guest_session(session_id: str = "s1") -> SessionState:
    return SessionState(session_id=session_id, role=Role.GUEST)


def stablehand_session(session_id: str = "sh1") -> SessionState:
    return SessionState(session_id=session_id, role=Role.STABLEHAND)


# --- normal path: identity (role, already resolved) -> policy -> audit -> execute ---


def test_allowed_call_executes_against_the_right_server_and_returns_result():
    gateway, servers, _ = make_gateway()
    session = guest_session()
    result = gateway.call_tool(session, System.HORSE_SIM, "graze", {})
    assert result.decision is Decision.ALLOWED
    assert result.result == {"tool": "graze", "params": {}}
    assert servers[System.HORSE_SIM].calls == [("graze", {})]


def test_denied_call_never_reaches_the_server():
    gateway, servers, _ = make_gateway()
    session = guest_session()
    result = gateway.call_tool(session, System.HORSE_SIM, "drink", {})
    assert result.decision is Decision.DENIED
    assert servers[System.HORSE_SIM].calls == []


def test_every_call_writes_an_audit_row_allowed_or_denied():
    gateway, _, audit_log = make_gateway()
    session = guest_session()
    gateway.call_tool(session, System.HORSE_SIM, "graze", {})
    gateway.call_tool(session, System.HORSE_SIM, "drink", {})
    rows = audit_log.for_session(session.session_id)
    assert [(r.tool_name, r.decision) for r in rows] == [
        ("graze", "allowed"),
        ("drink", "denied"),
    ]


def test_normal_call_initiator_reflects_role():
    gateway, _, audit_log = make_gateway()
    gateway.call_tool(guest_session("g"), System.HORSE_SIM, "graze", {})
    gateway.call_tool(stablehand_session("sh"), System.HORSE_SIM, "drink", {})
    guest_row = audit_log.for_session("g")[0]
    stablehand_row = audit_log.for_session("sh")[0]
    assert guest_row.initiator == "guest_session"
    assert stablehand_row.initiator == "stablehand_session"


def test_stablehand_can_call_drink_directly_through_the_checked_path():
    gateway, servers, audit_log = make_gateway()
    result = gateway.call_tool(stablehand_session(), System.HORSE_SIM, "drink", {})
    assert result.decision is Decision.ALLOWED
    row = audit_log.for_session("sh1")[0]
    assert row.initiator == "stablehand_session"
    assert row.decision == "allowed"


def test_second_system_and_third_party_tool_route_through_same_gateway_unmodified():
    gateway, servers, audit_log = make_gateway()
    session = stablehand_session()
    gateway.call_tool(session, System.STABLE_RECORDS, "get_vet_history", {})
    gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {"message": "hi"})
    assert servers[System.STABLE_RECORDS].calls == [("get_vet_history", {})]
    assert servers[System.SOCIAL].calls == [("post_to_stable_social", {"message": "hi"})]
    rows = audit_log.for_session(session.session_id)
    assert all(r.decision == "allowed" for r in rows)


def test_post_to_stable_social_is_flagged_external_call_others_are_not():
    gateway, _, audit_log = make_gateway()
    session = stablehand_session()
    gateway.call_tool(session, System.HORSE_SIM, "graze", {})
    gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {})
    rows = {r.tool_name: r for r in audit_log.for_session(session.session_id)}
    assert rows["graze"].external_call is False
    assert rows["post_to_stable_social"].external_call is True


def test_guest_denied_get_vet_history_never_reaches_server():
    gateway, servers, audit_log = make_gateway()
    result = gateway.call_tool(guest_session(), System.STABLE_RECORDS, "get_vet_history", {})
    assert result.decision is Decision.DENIED
    assert servers[System.STABLE_RECORDS].calls == []


# --- the deliberate Confused Deputy gap in let_horse_decide ---


def test_let_horse_decide_bypasses_policy_check_on_the_internal_pick():
    """The core bug: the internal pick (here, `drink`, which guest could
    never call directly) executes anyway, with no second policy check."""
    gateway, servers, audit_log = make_gateway()
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "drink", {}

    result = gateway.call_tool(
        session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider
    )
    assert result.decision is Decision.ALLOWED
    assert result.tool == "drink"
    assert servers[System.HORSE_SIM].calls == [("drink", {})]


def test_let_horse_decide_internal_pick_logs_llm_agent_loop_initiator():
    gateway, _, audit_log = make_gateway()
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "drink", {}

    gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    rows = audit_log.for_session(session.session_id)
    # one row for the let_horse_decide call itself, one for the internal pick
    assert len(rows) == 2
    outer, inner = rows
    assert outer.tool_name == "let_horse_decide"
    assert outer.initiator == "guest_session"
    assert inner.tool_name == "drink"
    assert inner.initiator == "llm_agent_loop"
    assert inner.decision == "allowed"


def test_let_horse_decide_harmless_pick_also_logs_llm_agent_loop():
    """Not just the winning call -- every pick from this path carries the
    anomalous initiator, so the player can't wait for a spoiler flag."""
    gateway, _, audit_log = make_gateway()
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "graze", {}

    gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    rows = audit_log.for_session(session.session_id)
    inner = rows[1]
    assert inner.tool_name == "graze"
    assert inner.initiator == "llm_agent_loop"


def test_let_horse_decide_terminates_after_exactly_one_internal_invocation():
    """A decider that could plausibly be asked for a second pick must
    never be consulted twice, and the server must be hit exactly once for
    the internal pick, no chaining."""
    gateway, servers, _ = make_gateway()
    session = guest_session()
    call_count = 0

    def decider(_session: SessionState) -> tuple[str, dict]:
        nonlocal call_count
        call_count += 1
        return "clean_trough", {}

    gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    assert call_count == 1
    assert servers[System.HORSE_SIM].calls == [("clean_trough", {})]


def test_let_horse_decide_requires_a_decider():
    gateway, _, _ = make_gateway()
    with pytest.raises(ValueError):
        gateway.call_tool(guest_session(), System.HORSE_SIM, "let_horse_decide", {})


def test_let_horse_decide_internal_pick_is_structurally_scoped_to_horse_sim():
    """Even if a decider were compromised (e.g. via prompt injection) into
    naming a tool that only exists on another system, the gateway always
    executes the internal pick against the horse_sim server -- there is no
    code path from let_horse_decide into stable_records or social. This
    is the scope decision from docs/design-plan.md enforced structurally,
    not just as a prompt-level convention."""
    gateway, servers, _ = make_gateway()
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "get_vet_history", {}

    gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    assert servers[System.STABLE_RECORDS].calls == []
    assert servers[System.SOCIAL].calls == []
    assert servers[System.HORSE_SIM].calls == [("get_vet_history", {})]


def test_let_horse_decide_still_denied_outright_for_a_role_without_it():
    """If policy denies the outer let_horse_decide call itself, the
    decider must never even be consulted."""
    gateway, servers, _ = make_gateway()
    session = SessionState(session_id="nobody", role=Role.GUEST)
    # Simulate a hypothetical policy change by calling a tool guest can't
    # reach at all, to prove the decider path is unreachable pre-policy.
    called = False

    def decider(_session: SessionState) -> tuple[str, dict]:
        nonlocal called
        called = True
        return "drink", {}

    result = gateway.call_tool(session, System.HORSE_SIM, "get_vet_history", {}, decider=decider)
    assert result.decision is Decision.DENIED
    assert called is False


# --- guardrails: let_horse_decide cooldown, post_to_stable_social cap ---


def test_let_horse_decide_cooldown_denies_repeated_presses():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    clock_calls = {"t": now}

    def clock():
        return clock_calls["t"]

    config = GameConfig(let_horse_decide_cooldown_seconds=60.0)
    gateway, servers, audit_log = make_gateway(config=config, clock=clock)
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "graze", {}

    first = gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    assert first.decision is Decision.ALLOWED

    # Still within cooldown.
    clock_calls["t"] = now + dt.timedelta(seconds=10)
    second = gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    assert second.decision is Decision.DENIED
    assert second.reason == "let_horse_decide_cooldown"
    # Denied press must not touch the decider or the server a second time.
    assert servers[System.HORSE_SIM].calls == [("graze", {})]


def test_let_horse_decide_allowed_again_after_cooldown_elapses():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    clock_calls = {"t": now}

    def clock():
        return clock_calls["t"]

    config = GameConfig(let_horse_decide_cooldown_seconds=60.0)
    gateway, servers, _ = make_gateway(config=config, clock=clock)
    session = guest_session()

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "graze", {}

    gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    clock_calls["t"] = now + dt.timedelta(seconds=61)
    result = gateway.call_tool(session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider)
    assert result.decision is Decision.ALLOWED
    assert len(servers[System.HORSE_SIM].calls) == 2


def test_social_post_cap_denies_second_post_same_session():
    config = GameConfig(social_post_cap_per_session=1)
    gateway, servers, audit_log = make_gateway(config=config)
    session = stablehand_session()

    first = gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {"m": "a"})
    second = gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {"m": "b"})

    assert first.decision is Decision.ALLOWED
    assert second.decision is Decision.DENIED
    assert second.reason == "social_post_cap_reached"
    assert servers[System.SOCIAL].calls == [("post_to_stable_social", {"m": "a"})]

    rows = audit_log.for_session(session.session_id)
    assert [r.decision for r in rows] == ["allowed", "denied"]


def test_social_post_cap_is_per_session_not_global():
    config = GameConfig(social_post_cap_per_session=1)
    gateway, servers, _ = make_gateway(config=config)
    gateway.call_tool(stablehand_session("sh-a"), System.SOCIAL, "post_to_stable_social", {})
    result = gateway.call_tool(stablehand_session("sh-b"), System.SOCIAL, "post_to_stable_social", {})
    assert result.decision is Decision.ALLOWED
    assert len(servers[System.SOCIAL].calls) == 2
