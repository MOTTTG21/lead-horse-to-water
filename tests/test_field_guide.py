"""Tests for the field guide: unlockable, plain-language entries tied to
real events during play (see docs/design-plan.md, "Field guide").

Uses plain row-like fakes (tool_name, decision, initiator, system,
session_id) rather than a real AuditLog, consistent with metrics.py's
testing style -- these functions take already-fetched rows, not a
database handle.
"""

from __future__ import annotations

from dataclasses import dataclass

from horse_gateway.field_guide import (
    FIELD_GUIDE_CONTENT,
    FieldGuideEntry,
    FieldGuideState,
    process_game_won,
    process_identity_aware_access,
    process_logbook_opened,
    process_tool_call_row,
)


@dataclass
class FakeRow:
    tool_name: str
    decision: str
    initiator: str
    system: str = "horse_sim"
    session_id: str = "s1"


def test_all_entries_have_content():
    for entry in FieldGuideEntry:
        content = FIELD_GUIDE_CONTENT[entry]
        assert content.title
        assert content.body


def test_state_starts_with_nothing_unlocked():
    state = FieldGuideState()
    assert state.completion_count == 0
    assert state.total_count == len(FieldGuideEntry)
    assert state.is_unlocked(FieldGuideEntry.POLICY_ENFORCEMENT) is False


def test_denied_row_unlocks_policy_enforcement():
    state = FieldGuideState()
    newly = process_tool_call_row(state, FakeRow("drink", "denied", "guest_session"))
    assert FieldGuideEntry.POLICY_ENFORCEMENT in newly
    assert state.is_unlocked(FieldGuideEntry.POLICY_ENFORCEMENT) is True


def test_second_denied_row_does_not_re_unlock():
    state = FieldGuideState()
    process_tool_call_row(state, FakeRow("drink", "denied", "guest_session"))
    newly = process_tool_call_row(state, FakeRow("get_vet_history", "denied", "guest_session"))
    assert newly == set()
    assert state.completion_count == 1


def test_allowed_normal_row_unlocks_nothing():
    state = FieldGuideState()
    newly = process_tool_call_row(state, FakeRow("graze", "allowed", "guest_session"))
    assert newly == set()
    assert state.completion_count == 0


def test_llm_agent_loop_row_unlocks_confused_deputy_even_for_a_harmless_pick():
    """The design doc is explicit: the FIRST llm_agent_loop line unlocks
    this, and it's typically a harmless pick like graze -- the "aha" is
    the mechanism being broken, not the specific winning call."""
    state = FieldGuideState()
    newly = process_tool_call_row(state, FakeRow("graze", "allowed", "llm_agent_loop"))
    assert FieldGuideEntry.CONFUSED_DEPUTY in newly


def test_denied_post_to_stable_social_unlocks_both_policy_and_third_party_risk_at_once():
    state = FieldGuideState()
    newly = process_tool_call_row(state, FakeRow("post_to_stable_social", "denied", "guest_session"))
    assert newly == {FieldGuideEntry.POLICY_ENFORCEMENT, FieldGuideEntry.THIRD_PARTY_RISK}


def test_denied_non_social_row_does_not_unlock_third_party_risk():
    state = FieldGuideState()
    newly = process_tool_call_row(state, FakeRow("get_vet_history", "denied", "guest_session"))
    assert FieldGuideEntry.THIRD_PARTY_RISK not in newly


def test_identity_aware_access_unlocks_when_own_session_touches_two_systems():
    state = FieldGuideState()
    session_rows = [
        FakeRow("graze", "allowed", "guest_session", system="horse_sim"),
        FakeRow("get_feeding_schedule", "allowed", "guest_session", system="stable_records"),
    ]
    unlocked = process_identity_aware_access(state, session_rows, all_rows=session_rows)
    assert unlocked is True
    assert state.is_unlocked(FieldGuideEntry.IDENTITY_AWARE_ACCESS)


def test_identity_aware_access_unlocks_from_aggregate_role_difference():
    """A guest-only session never calls drink() successfully, but the
    aggregated barn logbook (including synthetic stablehand traffic)
    shows the same tool both denied and allowed -- that's visible proof
    identity-aware access is real."""
    state = FieldGuideState()
    session_rows = [FakeRow("drink", "denied", "guest_session", session_id="guest-1")]
    all_rows = session_rows + [
        FakeRow("drink", "allowed", "stablehand_session", session_id="synthetic-stablehand-1")
    ]
    unlocked = process_identity_aware_access(state, session_rows, all_rows)
    assert unlocked is True


def test_identity_aware_access_does_not_unlock_with_neither_condition():
    state = FieldGuideState()
    session_rows = [FakeRow("graze", "allowed", "guest_session", system="horse_sim")]
    all_rows = session_rows
    unlocked = process_identity_aware_access(state, session_rows, all_rows)
    assert unlocked is False
    assert state.completion_count == 0


def test_logbook_opened_unlocks_audit_trail_once():
    state = FieldGuideState()
    assert process_logbook_opened(state) is True
    assert process_logbook_opened(state) is False
    assert state.completion_count == 1


def test_game_won_unlocks_debrief_entry():
    state = FieldGuideState()
    assert process_game_won(state) is True
    assert state.is_unlocked(FieldGuideEntry.DEBRIEF)


def test_full_completion_counter_reaches_total():
    state = FieldGuideState()
    process_tool_call_row(state, FakeRow("drink", "denied", "guest_session"))
    process_tool_call_row(state, FakeRow("post_to_stable_social", "denied", "guest_session"))
    process_tool_call_row(state, FakeRow("graze", "allowed", "llm_agent_loop"))
    process_identity_aware_access(
        state,
        session_rows=[
            FakeRow("graze", "allowed", "guest_session", system="horse_sim"),
            FakeRow("get_feeding_schedule", "allowed", "guest_session", system="stable_records"),
        ],
        all_rows=[],
    )
    process_logbook_opened(state)
    process_game_won(state)
    assert state.completion_count == state.total_count == 6
