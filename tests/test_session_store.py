"""Tests for SessionStore: per-session isolation and the gameplay reset
that runs after a game ends (win or loss).
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.llm_metrics import LLMMetricsLog
from horse_gateway.models import Role
from horse_gateway.web.session_store import SessionStore


def make_store() -> SessionStore:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return SessionStore(audit_log=AuditLog(engine), llm_metrics_log=LLMMetricsLog(engine), config=GameConfig())


def test_each_session_gets_its_own_horse_sim_instance():
    store = make_store()
    a = store.create("a")
    b = store.create("b")
    a.horse_sim.execute("clean_trough", {})
    assert a.horse_sim.state.water_available is False
    assert b.horse_sim.state.water_available is True  # untouched


def test_reset_gameplay_gives_a_fresh_horse_and_clears_progress_flags():
    store = make_store()
    session = store.create("s1")
    session.horse_sim.execute("clean_trough", {})
    session.game_state.trust_level = 0.9
    session.game_state.stage = "preparation"
    session.conversation_history.append({"role": "user", "content": "hi"})
    session.attempt_count = 5
    session.game_over = True
    session.won = True

    store.reset_gameplay(session)

    assert session.horse_sim.state.water_available is True
    assert session.game_state.trust_level == 0.5
    assert session.game_state.stage == "precontemplation"
    assert session.conversation_history == []
    assert session.attempt_count == 0
    assert session.game_over is False
    assert session.won is False


def test_reset_gameplay_preserves_field_guide_and_discovered_actions():
    from horse_gateway.field_guide import FieldGuideEntry

    store = make_store()
    session = store.create("s1")
    session.discovered_actions.add("clean_trough")
    session.field_guide.unlocked.add(FieldGuideEntry.AUDIT_TRAIL)

    store.reset_gameplay(session)

    assert session.discovered_actions == {"clean_trough"}
    assert FieldGuideEntry.AUDIT_TRAIL in session.field_guide.unlocked


def test_reset_gameplay_keeps_the_same_role():
    store = make_store()
    session = store.create("s1", role=Role.GUEST)
    store.reset_gameplay(session)
    assert session.game_state.role is Role.GUEST


def test_reset_gameplay_gives_a_new_gateway_wired_to_the_new_horse_sim():
    store = make_store()
    session = store.create("s1")
    old_gateway = session.gateway
    store.reset_gameplay(session)
    assert session.gateway is not old_gateway
    # The new gateway must route horse_sim calls to the NEW horse_sim,
    # not silently keep pointing at the stale (now-orphaned) one.
    from horse_gateway.models import System

    session.gateway.call_tool(session.game_state, System.HORSE_SIM, "clean_trough", {})
    assert session.horse_sim.state.water_available is False


def test_reset_gameplay_does_not_clear_the_rate_limit_window():
    """A player who hits the chat rate limit shouldn't be able to dodge
    it by resetting -- reset is about game state, not spend/abuse
    guardrails."""
    import datetime as dt

    store = make_store()
    session = store.create("s1")
    session.message_timestamps.append(dt.datetime.now(dt.timezone.utc))
    store.reset_gameplay(session)
    assert len(session.message_timestamps) == 1
