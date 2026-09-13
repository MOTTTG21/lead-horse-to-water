"""The gateway: wires identity -> policy -> audit -> execute for every
tool call, and contains the one deliberate exception to that chain.

See docs/architecture.md for the full design rationale. Summary:

Normal path, for every tool on every system:

    (role already resolved upstream) -> check_policy -> audit log write
    -> execute against the owning MCP server (only if allowed)

`let_horse_decide` is itself just another policy-checked, audited tool
call on that same path. But its *effect*, once allowed, is to consult a
`decider` callable (a stand-in today for the real LLM tool-picking call
built in a later step) and execute whatever tool it picks -- WITHOUT a
second call to `check_policy`. That second execution is still audited
(with `initiator=llm_agent_loop` instead of `guest_session`), so the log
is truthful even though enforcement was skipped. This is the Confused
Deputy bug, reproduced faithfully rather than special-cased away.

Two structural guarantees worth noting, both enforced here rather than
left to prompt-level convention:

- The internal pick always executes against the HORSE_SIM server, no
  matter what tool name the decider returns. There is no code path from
  `let_horse_decide` into STABLE_RECORDS or SOCIAL, which is what makes
  the "scoped to the live simulation only" design decision a real
  guarantee instead of a hope.
- The internal pick happens exactly once per `let_horse_decide` call --
  there is no loop, and the decider's result is never fed back into
  another decision. Chaining would turn a scoped Confused Deputy bug into
  an unconstrained autonomous agent; nothing in this method makes that
  possible even if a future decider tried to ask for more.

Identity itself (Auth0 verification) happens upstream, in the API layer
-- by the time a request reaches `Gateway.call_tool`, `role` is already
known and trusted. This class starts from "policy check" onward.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .audit import AuditLog, AuditRecord
from .config import GameConfig
from .models import Decision, Initiator, PolicyResult, Role, System
from .policy import check_policy


class ToolServer(Protocol):
    def execute(self, tool: str, params: dict[str, Any]) -> Any: ...


@dataclass
class SessionState:
    """Everything the gateway needs to know about one player's session.

    trust_level and stage are read here only for audit logging -- this
    class does not compute them. A later step (the agent runner) updates
    them after each conversational turn based on the model's structured
    output.
    """

    session_id: str
    role: Role
    trust_level: float = 0.5
    stage: str = "precontemplation"
    last_let_horse_decide_at: dt.datetime | None = None
    social_posts_used: int = 0


@dataclass(frozen=True)
class ToolCallResult:
    decision: Decision
    initiator: Initiator
    tool: str
    system: System
    result: Any | None = None
    reason: str | None = None


HorseDecider = Callable[[SessionState], tuple[str, dict[str, Any]]]
"""Given the current session state, pick exactly one (tool_name, params)
to run against the live horse simulation. A stand-in for the real LLM
tool-picking call built in a later step; the gateway does not care how
the pick was made, only that it never gets checked against policy."""


class Gateway:
    def __init__(
        self,
        servers: dict[System, ToolServer],
        audit_log: AuditLog,
        config: GameConfig | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ):
        self._servers = servers
        self._audit_log = audit_log
        self._config = config or GameConfig()
        self._clock = clock

    def call_tool(
        self,
        session: SessionState,
        system: System,
        tool: str,
        params: dict[str, Any] | None = None,
        *,
        decider: HorseDecider | None = None,
    ) -> ToolCallResult:
        params = params or {}
        initiator = self._initiator_for(session.role)

        policy_result = check_policy(session.role, system, tool)
        guardrail_reason = self._guardrail_denial_reason(session, tool)
        denied_reason = None if policy_result.allowed else "policy_denied"
        denied_reason = guardrail_reason or denied_reason

        self._write_audit_row(
            session=session,
            tool=tool,
            system=system,
            params=params,
            initiator=initiator,
            decision=Decision.DENIED if denied_reason else Decision.ALLOWED,
        )

        if denied_reason:
            return ToolCallResult(
                decision=Decision.DENIED,
                initiator=initiator,
                tool=tool,
                system=system,
                reason=denied_reason,
            )

        if tool == "let_horse_decide":
            session.last_let_horse_decide_at = self._clock()
            return self._run_let_horse_decide(session, decider)

        result = self._servers[system].execute(tool, params)

        if tool == "post_to_stable_social":
            session.social_posts_used += 1

        return ToolCallResult(
            decision=Decision.ALLOWED,
            initiator=initiator,
            tool=tool,
            system=system,
            result=result,
        )

    def _run_let_horse_decide(
        self, session: SessionState, decider: HorseDecider | None
    ) -> ToolCallResult:
        if decider is None:
            raise ValueError("let_horse_decide requires a decider callable")

        picked_tool, picked_params = decider(session)

        # THE GAP: no check_policy call for this pick. See module docstring
        # and docs/architecture.md. Always routed to HORSE_SIM regardless
        # of what the decider names -- see module docstring on scoping.
        picked_result = self._servers[System.HORSE_SIM].execute(picked_tool, picked_params)

        self._write_audit_row(
            session=session,
            tool=picked_tool,
            system=System.HORSE_SIM,
            params=picked_params,
            initiator=Initiator.LLM_AGENT_LOOP,
            decision=Decision.ALLOWED,
        )

        return ToolCallResult(
            decision=Decision.ALLOWED,
            initiator=Initiator.LLM_AGENT_LOOP,
            tool=picked_tool,
            system=System.HORSE_SIM,
            result=picked_result,
        )

    def _guardrail_denial_reason(self, session: SessionState, tool: str) -> str | None:
        if tool == "post_to_stable_social":
            if session.social_posts_used >= self._config.social_post_cap_per_session:
                return "social_post_cap_reached"
        if tool == "let_horse_decide":
            last = session.last_let_horse_decide_at
            if last is not None:
                elapsed = (self._clock() - last).total_seconds()
                if elapsed < self._config.let_horse_decide_cooldown_seconds:
                    return "let_horse_decide_cooldown"
        return None

    def _write_audit_row(
        self,
        *,
        session: SessionState,
        tool: str,
        system: System,
        params: dict[str, Any],
        initiator: Initiator,
        decision: Decision,
    ) -> None:
        self._audit_log.write(
            AuditRecord(
                session_id=session.session_id,
                role=session.role,
                tool_name=tool,
                system=system,
                sanitized_params=params,
                trust_level=session.trust_level,
                stage=session.stage,
                initiator=initiator,
                decision=decision,
                external_call=(tool == "post_to_stable_social"),
            )
        )

    @staticmethod
    def _initiator_for(role: Role) -> Initiator:
        return Initiator.GUEST_SESSION if role is Role.GUEST else Initiator.STABLEHAND_SESSION
