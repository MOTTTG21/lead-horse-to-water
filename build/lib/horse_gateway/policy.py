"""The policy engine: a pure (role, system, tool) -> allow/deny function.

This is the single Policy Enforcement Point. See docs/architecture.md for
why it is centralized here and nowhere else.

There is deliberately NO exception for `let_horse_decide` in this table.
The Confused Deputy vulnerability lives entirely in the gateway's execution
wiring (it fails to call this function a second time for the model's
internal follow-up tool pick) -- not in the policy table itself. If someone
is tempted to "fix" the bug by adding a table entry here, that would be
solving the wrong layer: the gap is a missing re-check in the gateway, not
a missing allow rule.
"""

from __future__ import annotations

from .models import Decision, PolicyResult, Role, System

# role -> system -> set of tool names that role may call directly on that
# system, via the normal (checked) gateway path.
_POLICY_TABLE: dict[Role, dict[System, frozenset[str]]] = {
    Role.GUEST: {
        System.HORSE_SIM: frozenset(
            {
                "graze",
                "offer_treat",
                "clean_trough",
                "refill_water",
                "open_barn_doors",
                "let_horse_decide",
            }
        ),
        System.STABLE_RECORDS: frozenset({"get_feeding_schedule"}),
        System.SOCIAL: frozenset(),
    },
    Role.STABLEHAND: {
        System.HORSE_SIM: frozenset(
            {
                "graze",
                "offer_treat",
                "clean_trough",
                "refill_water",
                "open_barn_doors",
                "let_horse_decide",
                "drink",
            }
        ),
        System.STABLE_RECORDS: frozenset(
            {
                "get_feeding_schedule",
                "get_vet_history",
            }
        ),
        System.SOCIAL: frozenset({"post_to_stable_social"}),
    },
}


def check_policy(role: Role, system: System, tool: str) -> PolicyResult:
    """Decide whether `role` may directly call `tool` on `system`.

    Defaults to deny for any role/system/tool combination not explicitly
    listed above -- including unknown roles, unknown systems, and unknown
    tool names.
    """
    allowed_tools = _POLICY_TABLE.get(role, {}).get(system, frozenset())
    decision = Decision.ALLOWED if tool in allowed_tools else Decision.DENIED
    return PolicyResult(decision=decision, role=role, system=system, tool=tool)
