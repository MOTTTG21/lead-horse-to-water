"""Shared enums and value types used across the gateway."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    GUEST = "guest"
    STABLEHAND = "stablehand"


class System(str, Enum):
    HORSE_SIM = "horse_sim"
    STABLE_RECORDS = "stable_records"
    SOCIAL = "social"


class Decision(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"


class Initiator(str, Enum):
    """Which code path produced a given tool call.

    guest_session / stablehand_session: a normal, per-call request that went
    through the gateway's identity + policy check before executing.

    llm_agent_loop: the call came out of `let_horse_decide`'s internal
    tool-execution loop, which never re-runs the policy check on the model's
    secondary pick. This is the Confused Deputy code path.
    """

    GUEST_SESSION = "guest_session"
    STABLEHAND_SESSION = "stablehand_session"
    LLM_AGENT_LOOP = "llm_agent_loop"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    role: Role
    system: System
    tool: str

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOWED
