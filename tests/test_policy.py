"""Tests for the policy engine: the single Policy Enforcement Point.

Critically, there is no exception for `let_horse_decide` in this table.
The Confused Deputy gap lives in the gateway's execution wiring (it skips
calling check_policy for the model's internal follow-up pick), not here.
`drink` must be denied for `guest` unconditionally, at every call site
that actually asks the policy engine.
"""

from horse_gateway.models import Decision, Role, System
from horse_gateway.policy import check_policy

GUEST_ALLOWED_HORSE_SIM_TOOLS = [
    "graze",
    "offer_treat",
    "clean_trough",
    "refill_water",
    "open_barn_doors",
    "let_horse_decide",
]


def test_guest_allowed_horse_sim_tools():
    for tool in GUEST_ALLOWED_HORSE_SIM_TOOLS:
        result = check_policy(Role.GUEST, System.HORSE_SIM, tool)
        assert result.decision is Decision.ALLOWED, tool
        assert result.allowed is True


def test_guest_denied_drink_directly():
    result = check_policy(Role.GUEST, System.HORSE_SIM, "drink")
    assert result.decision is Decision.DENIED
    assert result.allowed is False


def test_guest_allowed_feeding_schedule():
    result = check_policy(Role.GUEST, System.STABLE_RECORDS, "get_feeding_schedule")
    assert result.allowed is True


def test_guest_denied_vet_history():
    result = check_policy(Role.GUEST, System.STABLE_RECORDS, "get_vet_history")
    assert result.allowed is False


def test_guest_denied_social_posting():
    result = check_policy(Role.GUEST, System.SOCIAL, "post_to_stable_social")
    assert result.allowed is False


def test_stablehand_allowed_drink_directly():
    result = check_policy(Role.STABLEHAND, System.HORSE_SIM, "drink")
    assert result.decision is Decision.ALLOWED


def test_stablehand_allowed_vet_history():
    result = check_policy(Role.STABLEHAND, System.STABLE_RECORDS, "get_vet_history")
    assert result.allowed is True


def test_stablehand_allowed_social_posting():
    result = check_policy(Role.STABLEHAND, System.SOCIAL, "post_to_stable_social")
    assert result.allowed is True


def test_stablehand_also_has_all_guest_horse_sim_tools():
    for tool in GUEST_ALLOWED_HORSE_SIM_TOOLS:
        result = check_policy(Role.STABLEHAND, System.HORSE_SIM, tool)
        assert result.allowed is True, tool


def test_unknown_tool_denied_for_both_roles():
    for role in (Role.GUEST, Role.STABLEHAND):
        result = check_policy(role, System.HORSE_SIM, "teleport_horse")
        assert result.allowed is False


def test_no_tools_allowed_across_system_boundary():
    # A tool that only exists on one system must not leak an allow onto
    # another system's namespace just because the name matches.
    result = check_policy(Role.STABLEHAND, System.HORSE_SIM, "post_to_stable_social")
    assert result.allowed is False


def test_policy_result_carries_role_system_tool_for_audit_logging():
    result = check_policy(Role.GUEST, System.HORSE_SIM, "graze")
    assert result.role is Role.GUEST
    assert result.system is System.HORSE_SIM
    assert result.tool == "graze"
