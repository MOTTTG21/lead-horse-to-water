"""In-memory per-player session state for the web app.

Each PlayerSession gets its OWN HorseSimulationServer instance and its
own Gateway wired to it -- physical barn state (thirst, water,
temperature) must be isolated per player (see docs/design-plan.md,
"Roles": "each player's actual horse ... stays strictly isolated to
their own session"). Gateway itself has no notion of "sessions" beyond
the SessionState passed into call_tool; isolation here comes entirely
from never sharing a HorseSimulationServer instance across two
PlayerSessions. STABLE_RECORDS and SOCIAL servers ARE shared across all
sessions (global barn records, not per-player state), as is the audit
log -- so the barn logbook still aggregates everyone's activity in one
place, per the design.

This is a simple in-memory dict, not a persistent session store --
acceptable for a single-process dev/demo deployment; a real multi-
instance production deployment would move this to Redis or similar, but
that's out of scope for what this project is demonstrating.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..audit import AuditLog
from ..config import GameConfig
from ..field_guide import FieldGuideState
from ..gateway import Gateway, SessionState
from ..horse_sim_server import HorseSimulationServer
from ..llm_metrics import LLMMetricsLog
from ..models import Role, System
from ..social_server import StableSocialServer
from ..stable_records_server import StableRecordsServer


@dataclass
class PlayerSession:
    session_id: str
    gateway: Gateway
    horse_sim: HorseSimulationServer
    game_state: SessionState
    field_guide: FieldGuideState = field(default_factory=FieldGuideState)
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    attempt_count: int = 0
    game_over: bool = False
    won: bool = False


class SessionStore:
    """Shared, process-wide dependencies (audit log, LLM metrics log,
    the global stable-records/social servers, config) live here once;
    each `get_or_create` call builds a new, fully isolated
    PlayerSession on top of them."""

    def __init__(
        self,
        audit_log: AuditLog,
        llm_metrics_log: LLMMetricsLog,
        config: GameConfig | None = None,
        stable_records_server: StableRecordsServer | None = None,
        social_server: StableSocialServer | None = None,
    ):
        self.audit_log = audit_log
        self.llm_metrics_log = llm_metrics_log
        self.config = config or GameConfig()
        self.stable_records_server = stable_records_server or StableRecordsServer()
        self.social_server = social_server or StableSocialServer()
        self._sessions: dict[str, PlayerSession] = {}

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def create(self, session_id: str, role: Role = Role.GUEST) -> PlayerSession:
        horse_sim = HorseSimulationServer(config=self.config)
        gateway = Gateway(
            servers={
                System.HORSE_SIM: horse_sim,
                System.STABLE_RECORDS: self.stable_records_server,
                System.SOCIAL: self.social_server,
            },
            audit_log=self.audit_log,
            config=self.config,
        )
        session = PlayerSession(
            session_id=session_id,
            gateway=gateway,
            horse_sim=horse_sim,
            game_state=SessionState(session_id=session_id, role=role),
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> PlayerSession | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> PlayerSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        new_id = session_id or self.new_session_id()
        return self.create(new_id)
