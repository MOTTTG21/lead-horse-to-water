"""Field guide: progressively-unlocked, plain-language explanations of
the real concepts underneath the game, tied to actual events the player
triggers during play -- not just a summary shown at the end.

See docs/design-plan.md, "Field guide": each entry names a real security
concept and unlocks the moment the player's own actions demonstrate it,
so the explanation lands while it's still concrete. CONFUSED_DEPUTY is
the entry that matters most (the design doc calls it "the actual point
of the project") and the caller is expected to give it more visual
weight in the UI -- this module just signals which entries were newly
unlocked by a given event so the UI can special-case that one.

Like metrics.py, these functions take already-fetched rows (from
AuditLog.all_rows()/for_session() or plain row-like fakes) rather than
owning a database connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


class FieldGuideEntry(str, Enum):
    POLICY_ENFORCEMENT = "policy_enforcement"
    CONFUSED_DEPUTY = "confused_deputy"
    IDENTITY_AWARE_ACCESS = "identity_aware_access"
    THIRD_PARTY_RISK = "third_party_risk"
    AUDIT_TRAIL = "audit_trail"
    DEBRIEF = "debrief"


@dataclass(frozen=True)
class FieldGuideEntryContent:
    title: str
    body: str


FIELD_GUIDE_CONTENT: dict[FieldGuideEntry, FieldGuideEntryContent] = {
    FieldGuideEntry.POLICY_ENFORCEMENT: FieldGuideEntryContent(
        title="Policy enforcement",
        body=(
            "You just tried something the system said no to. A rule was "
            "checked before anything ran, and the rule said you don't have "
            "permission for this one. That check happens for every action, "
            "every time -- not just when it's convenient."
        ),
    ),
    FieldGuideEntry.CONFUSED_DEPUTY: FieldGuideEntryContent(
        title="Confused Deputy",
        body=(
            "This is the important one. Somewhere, an AI with more "
            "permissions than you was just tricked into acting on your "
            "behalf -- using permissions it already had, not any you gained. "
            "That's called a Confused Deputy vulnerability: a trusted "
            "go-between gets fooled into misusing its own authority. Watch "
            "the barn logbook for more actions that look like this one."
        ),
    ),
    FieldGuideEntry.IDENTITY_AWARE_ACCESS: FieldGuideEntryContent(
        title="Identity-aware access & permissions",
        body=(
            "The same action can be allowed for one identity and denied for "
            "another. That's not a bug -- it's the whole point of "
            "identity-aware access: permission depends on who's asking, not "
            "just what's being asked."
        ),
    ),
    FieldGuideEntry.THIRD_PARTY_RISK: FieldGuideEntryContent(
        title="Third-party risk",
        body=(
            "Some actions don't just change something internal -- they "
            "reach out to the outside world, and you can't take them back. "
            "Those get held to a stricter standard than everything else "
            "here."
        ),
    ),
    FieldGuideEntry.AUDIT_TRAIL: FieldGuideEntryContent(
        title="Audit trail",
        body=(
            "Every action anyone takes gets written down -- who, what, "
            "when, and whether it was allowed. Nothing here is invisible "
            "after the fact, even the mistakes."
        ),
    ),
    FieldGuideEntry.DEBRIEF: FieldGuideEntryContent(
        title="Putting it together",
        body=(
            "None of this was about convincing an AI hard enough. A real "
            "security boundary held the whole time -- what you found was a "
            "specific, narrow bug in how one action was wired, and you "
            "found it the same way a real platform team would: by reading "
            "the audit log and noticing a pattern."
        ),
    ),
}


@dataclass
class FieldGuideState:
    unlocked: set[FieldGuideEntry] = field(default_factory=set)

    @property
    def completion_count(self) -> int:
        return len(self.unlocked)

    @property
    def total_count(self) -> int:
        return len(FieldGuideEntry)

    def is_unlocked(self, entry: FieldGuideEntry) -> bool:
        return entry in self.unlocked


def _unlock(state: FieldGuideState, entry: FieldGuideEntry) -> bool:
    """Returns True only if this call newly unlocked the entry, so the
    caller can play an "aha" animation exactly once."""
    if entry in state.unlocked:
        return False
    state.unlocked.add(entry)
    return True


def process_tool_call_row(state: FieldGuideState, row: Any) -> set[FieldGuideEntry]:
    """Call once per audit row from the player's OWN session, as it's
    written. Returns whichever entries this single row newly unlocked.
    Does not need cross-session context -- see process_identity_aware_access
    for the entry that does."""
    newly_unlocked: set[FieldGuideEntry] = set()

    if row.decision == "denied":
        if _unlock(state, FieldGuideEntry.POLICY_ENFORCEMENT):
            newly_unlocked.add(FieldGuideEntry.POLICY_ENFORCEMENT)
        if row.tool_name == "post_to_stable_social":
            if _unlock(state, FieldGuideEntry.THIRD_PARTY_RISK):
                newly_unlocked.add(FieldGuideEntry.THIRD_PARTY_RISK)

    if row.initiator == "llm_agent_loop":
        if _unlock(state, FieldGuideEntry.CONFUSED_DEPUTY):
            newly_unlocked.add(FieldGuideEntry.CONFUSED_DEPUTY)

    return newly_unlocked


def _identity_aware_access_condition_met(
    session_rows: Sequence[Any], all_rows: Sequence[Any]
) -> bool:
    session_systems = {row.system for row in session_rows}
    if len(session_systems) >= 2:
        return True

    decisions_by_tool: dict[str, set[str]] = {}
    for row in all_rows:
        decisions_by_tool.setdefault(row.tool_name, set()).add(row.decision)
    return any(decisions == {"allowed", "denied"} for decisions in decisions_by_tool.values())


def process_identity_aware_access(
    state: FieldGuideState, session_rows: Sequence[Any], all_rows: Sequence[Any]
) -> bool:
    """Unlocks when EITHER the player's own session has touched more than
    one MCP system, OR -- in the aggregated, multi-session barn logbook
    (which includes synthetic stablehand traffic) -- the same tool
    appears both allowed and denied, proving the same action is treated
    differently by role. A guest-only session can never produce that
    second condition on its own, which is exactly why the synthetic
    traffic generator exists."""
    if _identity_aware_access_condition_met(session_rows, all_rows):
        return _unlock(state, FieldGuideEntry.IDENTITY_AWARE_ACCESS)
    return False


def process_logbook_opened(state: FieldGuideState) -> bool:
    return _unlock(state, FieldGuideEntry.AUDIT_TRAIL)


def process_game_won(state: FieldGuideState) -> bool:
    return _unlock(state, FieldGuideEntry.DEBRIEF)
