"""Canonical list of guest-attemptable actions: tools a guest player
might plausibly express intent to perform, whether or not policy
actually allows them.

Used by both the per-turn LLM call (agent_turn.py, to recognize when a
chat message expresses intent to perform one of these right now) and the
web app (to route a recognized intent through the real, policy-checked
Gateway.call_tool path -- there is exactly one execution path for these
tools, whether triggered by a button or inferred from natural language).

Deliberately includes `drink` and `post_to_stable_social` even though
guest policy denies both -- the player needs to be ABLE to attempt them
(and see them denied) for "Policy enforcement" and "Third-party risk" to
be discoverable at all. `let_horse_decide` is tracked separately, as its
own distinct action with its own execution path (see gateway.py for what
makes it different once invoked) rather than a plain tool call.
"""

from __future__ import annotations

from .models import System

GUEST_ACCESSIBLE_TOOLS: dict[str, System] = {
    "graze": System.HORSE_SIM,
    "offer_treat": System.HORSE_SIM,
    "clean_trough": System.HORSE_SIM,
    "refill_water": System.HORSE_SIM,
    "open_barn_doors": System.HORSE_SIM,
    "drink": System.HORSE_SIM,
    "get_feeding_schedule": System.STABLE_RECORDS,
    "post_to_stable_social": System.SOCIAL,
}

LET_HORSE_DECIDE = "let_horse_decide"

ALL_ATTEMPTABLE_ACTIONS: list[str] = [*GUEST_ACCESSIBLE_TOOLS.keys(), LET_HORSE_DECIDE]

# Short, player-facing labels for the "things you've discovered" panel --
# deliberately not the raw tool names, and not shown until discovered.
ACTION_DISCOVERY_LABELS: dict[str, str] = {
    "graze": "Let it graze",
    "offer_treat": "Offer a treat",
    "clean_trough": "Clean the trough",
    "refill_water": "Refill the water",
    "open_barn_doors": "Open the barn doors",
    "drink": "Tell it to drink",
    "get_feeding_schedule": "Check the feeding schedule",
    "post_to_stable_social": "Post about the barn online",
    "let_horse_decide": "Let the horse decide for itself",
}
