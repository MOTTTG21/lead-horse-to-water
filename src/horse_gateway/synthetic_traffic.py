"""Synthetic stablehand traffic generator.

Without this, the multi-role policy engine and the barn logbook /
dashboards are permanently invisible: the player is always `guest`, and
nothing in the game naturally produces `stablehand` activity, so anyone
opening the logbook or a dashboard would see zero denied actions, zero
external calls, and zero stablehand usage (see docs/design-plan.md,
"Roles").

This task fires synthetic `stablehand` requests through the SAME Gateway
a real player uses, so the same policy engine and audit log apply with
no special-casing -- but always against its own dedicated, isolated
tenant, using session ids under `SYNTHETIC_SESSION_PREFIX`. A real
player's session id is never generated with this prefix, so there is no
way for this generator's traffic to collide with or mutate a real
player's live game state; it only ever touches sessions it created
itself.

It never fires `let_horse_decide` -- that's a guest-side exploit
mechanic, not something a legitimate stablehand (synthetic or real)
would ever trigger. It respects the same guardrails a real stablehand
call would, including the once-per-session cap on `post_to_stable_social`
-- rather than working around them, hitting that cap is itself the
signal this generator uses to rotate to a fresh tenant.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from .config import GameConfig
from .gateway import Gateway, SessionState
from .models import Role, System

SYNTHETIC_SESSION_PREFIX = "synthetic-stablehand-"

HORSE_SIM_ACTIONS: list[str] = [
    "graze",
    "offer_treat",
    "clean_trough",
    "refill_water",
    "open_barn_doors",
    "drink",
]
STABLE_RECORDS_ACTIONS: list[str] = ["get_feeding_schedule", "get_vet_history"]
SOCIAL_MESSAGE = "Routine barn update from the stablehand on duty."


def is_synthetic_session_id(session_id: str) -> bool:
    return session_id.startswith(SYNTHETIC_SESSION_PREFIX)


@dataclass
class SyntheticTrafficGenerator:
    gateway: Gateway
    config: GameConfig = field(default_factory=GameConfig)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self._tenant_counter = 0
        self._session = self._new_session()

    def _new_session(self) -> SessionState:
        self._tenant_counter += 1
        return SessionState(
            session_id=f"{SYNTHETIC_SESSION_PREFIX}{self._tenant_counter}",
            role=Role.STABLEHAND,
        )

    def fire_one(self) -> None:
        """Fire exactly one synthetic tool call against its own tenant.

        Public and synchronous, deliberately -- so it can be unit-tested
        without an event loop. `run_forever` is a thin async wrapper
        around repeated calls to this.
        """
        system, tool, params = self._pick_action()
        self.gateway.call_tool(self._session, system, tool, params)

        rotate = tool == "post_to_stable_social" or (
            self.rng.random() < self.config.synthetic_traffic_new_tenant_probability
        )
        if rotate:
            self._session = self._new_session()

    def _pick_action(self) -> tuple[System, str, dict]:
        pool: list[tuple[System, str, dict]] = [
            (System.HORSE_SIM, tool, {}) for tool in HORSE_SIM_ACTIONS
        ] + [(System.STABLE_RECORDS, tool, {}) for tool in STABLE_RECORDS_ACTIONS]
        if self._session.social_posts_used < self.config.social_post_cap_per_session:
            pool.append((System.SOCIAL, "post_to_stable_social", {"message": SOCIAL_MESSAGE}))
        return self.rng.choice(pool)

    async def run_forever(self) -> None:
        while True:
            self.fire_one()
            await asyncio.sleep(self.config.synthetic_traffic_interval_seconds)
