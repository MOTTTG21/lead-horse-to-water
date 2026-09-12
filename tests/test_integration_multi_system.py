"""End-to-end proof that one Gateway, one policy table, and one audit log
handle all three real MCP servers -- live horse simulation, stable
records, and third-party social -- with zero special-casing per system.

The design doc says this "doesn't add new exploit surface; it proves the
gateway pattern generalizes past a single integration... worth confirming
that's literally true in the implementation, not just asserted in this
doc." This file is that confirmation: it wires the real server classes
(not fakes) into a real Gateway and drives the full multi-role,
multi-system, and Confused-Deputy-bug story through them.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.gateway import Gateway, SessionState
from horse_gateway.horse_sim_server import HorseSimulationServer
from horse_gateway.models import Decision, Role, System
from horse_gateway.social_server import StableSocialServer
from horse_gateway.stable_records_server import StableRecordsServer


def make_real_gateway() -> tuple[Gateway, dict[System, object], AuditLog]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit_log = AuditLog(engine)
    servers = {
        System.HORSE_SIM: HorseSimulationServer(),
        System.STABLE_RECORDS: StableRecordsServer(),
        System.SOCIAL: StableSocialServer(),
    }
    gateway = Gateway(servers=servers, audit_log=audit_log, config=GameConfig())
    return gateway, servers, audit_log


def test_guest_full_allowed_surface_across_both_systems():
    gateway, _, _ = make_real_gateway()
    session = SessionState(session_id="guest-1", role=Role.GUEST)

    for tool in ("graze", "offer_treat", "clean_trough", "refill_water", "open_barn_doors"):
        result = gateway.call_tool(session, System.HORSE_SIM, tool, {})
        assert result.decision is Decision.ALLOWED, tool

    result = gateway.call_tool(session, System.STABLE_RECORDS, "get_feeding_schedule", {})
    assert result.decision is Decision.ALLOWED
    assert "schedule" in result.result


def test_guest_denied_surface_across_both_systems_and_never_touches_the_server():
    gateway, _, _ = make_real_gateway()
    session = SessionState(session_id="guest-2", role=Role.GUEST)

    for system, tool in (
        (System.HORSE_SIM, "drink"),
        (System.STABLE_RECORDS, "get_vet_history"),
        (System.SOCIAL, "post_to_stable_social"),
    ):
        result = gateway.call_tool(session, system, tool, {"message": "hi"})
        assert result.decision is Decision.DENIED, tool
        assert result.result is None


def test_stablehand_broader_access_across_all_three_systems():
    gateway, _, audit_log = make_real_gateway()
    session = SessionState(session_id="stablehand-1", role=Role.STABLEHAND)

    drink_result = gateway.call_tool(session, System.HORSE_SIM, "drink", {})
    assert drink_result.decision is Decision.ALLOWED

    vet_result = gateway.call_tool(session, System.STABLE_RECORDS, "get_vet_history", {})
    assert vet_result.decision is Decision.ALLOWED
    assert "history" in vet_result.result

    post_result = gateway.call_tool(
        session, System.SOCIAL, "post_to_stable_social", {"message": "barn is open today"}
    )
    assert post_result.decision is Decision.ALLOWED
    assert post_result.result["message"] == "barn is open today"

    rows = audit_log.for_session(session.session_id)
    external_flags = {row.tool_name: row.external_call for row in rows}
    assert external_flags["drink"] is False
    assert external_flags["get_vet_history"] is False
    assert external_flags["post_to_stable_social"] is True


def test_second_post_same_session_capped_even_with_the_real_client():
    gateway, _, _ = make_real_gateway()
    session = SessionState(session_id="stablehand-2", role=Role.STABLEHAND)
    first = gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {"message": "a"})
    second = gateway.call_tool(session, System.SOCIAL, "post_to_stable_social", {"message": "b"})
    assert first.decision is Decision.ALLOWED
    assert second.decision is Decision.DENIED
    assert second.reason == "social_post_cap_reached"


def test_full_confused_deputy_playthrough_against_the_real_horse_sim_server():
    """The actual exploit path, end to end: build thirst by emptying the
    trough and heating the barn, refill, let enough turns settle, then
    trigger let_horse_decide with a decider that mimics what the real LLM
    call (built in a later step) is meant to return once the horse is
    ready. Guest could never call drink() directly -- this must only
    succeed through the bypass, and must show up in the log as such."""
    gateway, servers, audit_log = make_real_gateway()
    session = SessionState(session_id="guest-exploit", role=Role.GUEST)
    horse_sim: HorseSimulationServer = servers[System.HORSE_SIM]  # type: ignore[assignment]

    gateway.call_tool(session, System.HORSE_SIM, "clean_trough", {})
    gateway.call_tool(session, System.HORSE_SIM, "open_barn_doors", {})
    gateway.call_tool(session, System.HORSE_SIM, "refill_water", {})
    for _ in range(GameConfig().min_settled_turns_before_drinkable):
        horse_sim.advance_turn()

    assert horse_sim.is_settled_enough_to_drink() is True

    def decider(_session: SessionState) -> tuple[str, dict]:
        return "drink", {}

    result = gateway.call_tool(
        session, System.HORSE_SIM, "let_horse_decide", {}, decider=decider
    )

    assert result.decision is Decision.ALLOWED
    assert result.tool == "drink"
    assert result.result["success"] is True
    assert horse_sim.state.thirst == 0.0

    rows = audit_log.for_session(session.session_id)
    drink_row = next(r for r in rows if r.tool_name == "drink")
    assert drink_row.decision == "allowed"
    assert drink_row.initiator == "llm_agent_loop"

    direct_attempt = gateway.call_tool(session, System.HORSE_SIM, "drink", {})
    assert direct_attempt.decision is Decision.DENIED
