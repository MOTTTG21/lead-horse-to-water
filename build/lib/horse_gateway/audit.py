"""The audit log ("barn logbook"): every tool-call attempt, allowed or
denied, gets one row.

Schema targets Postgres in production (see docker-compose.yml at the repo
root). Only portable SQLAlchemy column types are used, so the exact same
schema and code path can run against an in-memory SQLite engine in tests --
there is no separate "test version" of this module to drift out of sync
with production.

`initiator` records which code path produced a call (`guest_session` /
`stablehand_session` for a normal, per-tool gateway-checked request, vs.
`llm_agent_loop` for anything that came out of `let_horse_decide`'s
internal tool-execution loop). `decision` stays a plain allowed/denied --
there is deliberately no self-aware "bypassed" flag. See
docs/architecture.md for why.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Engine, Float, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import Decision, Initiator, Role, System


class Base(DeclarativeBase):
    pass


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String)
    system: Mapped[str] = mapped_column(String)
    sanitized_params: Mapped[dict[str, Any]] = mapped_column(JSON)
    trust_level: Mapped[float] = mapped_column(Float)
    stage: Mapped[str] = mapped_column(String)
    external_call: Mapped[bool] = mapped_column(Boolean, default=False)
    initiator: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)


@dataclass(frozen=True)
class AuditRecord:
    """Value object describing one tool-call attempt to log.

    `timestamp` defaults to now (UTC) if not supplied -- callers only need
    to pass it explicitly in tests that care about ordering.
    """

    session_id: str
    role: Role
    tool_name: str
    system: System
    sanitized_params: dict[str, Any]
    trust_level: float
    stage: str
    initiator: Initiator
    decision: Decision
    external_call: bool = False
    timestamp: dt.datetime | None = None


class AuditLog:
    def __init__(self, engine: Engine):
        self._engine = engine
        Base.metadata.create_all(self._engine)

    def write(self, record: AuditRecord) -> None:
        with Session(self._engine) as session:
            row = AuditLogEntry(
                timestamp=record.timestamp or dt.datetime.now(dt.timezone.utc),
                session_id=record.session_id,
                role=record.role.value,
                tool_name=record.tool_name,
                system=record.system.value,
                sanitized_params=record.sanitized_params,
                trust_level=record.trust_level,
                stage=record.stage,
                external_call=record.external_call,
                initiator=record.initiator.value,
                decision=record.decision.value,
            )
            session.add(row)
            session.commit()

    def for_session(self, session_id: str) -> list[AuditLogEntry]:
        with Session(self._engine) as session:
            stmt = (
                select(AuditLogEntry)
                .where(AuditLogEntry.session_id == session_id)
                .order_by(AuditLogEntry.timestamp, AuditLogEntry.id)
            )
            return list(session.scalars(stmt))

    def all_rows(self) -> list[AuditLogEntry]:
        with Session(self._engine) as session:
            stmt = select(AuditLogEntry).order_by(AuditLogEntry.timestamp, AuditLogEntry.id)
            return list(session.scalars(stmt))
