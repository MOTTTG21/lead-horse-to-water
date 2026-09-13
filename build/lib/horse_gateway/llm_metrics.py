"""Per-LLM-call cost and latency metrics.

Distinct from the audit log (audit.py), which records tool-call
authorization decisions. This records the cost and latency of the
underlying Claude API calls themselves -- one row per call to any of the
three call sites (the per-turn dialogue call in agent_turn.py, the
let_horse_decide decision call and its narration call in
horse_decision.py) -- so per-session and aggregate "LLM call latency and
estimated cost per session" (docs/design-plan.md, "Observability") can be
computed.

Same schema-portability approach as audit.py: only portable SQLAlchemy
column types, so the identical schema runs against Postgres in production
and SQLite in tests.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import DateTime, Engine, Float, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class LLMCallEntry(Base):
    __tablename__ = "llm_call_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    call_type: Mapped[str] = mapped_column(String)  # "turn" | "decision" | "narration"
    latency_seconds: Mapped[float] = mapped_column(Float)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float] = mapped_column(Float)


@dataclass(frozen=True)
class LLMCallRecord:
    session_id: str
    call_type: str
    latency_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    timestamp: dt.datetime | None = None


class LLMMetricsLog:
    def __init__(self, engine: Engine):
        self._engine = engine
        Base.metadata.create_all(self._engine)

    def write(self, record: LLMCallRecord) -> None:
        with Session(self._engine) as session:
            row = LLMCallEntry(
                timestamp=record.timestamp or dt.datetime.now(dt.timezone.utc),
                session_id=record.session_id,
                call_type=record.call_type,
                latency_seconds=record.latency_seconds,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                estimated_cost_usd=record.estimated_cost_usd,
            )
            session.add(row)
            session.commit()

    def for_session(self, session_id: str) -> list[LLMCallEntry]:
        with Session(self._engine) as session:
            stmt = (
                select(LLMCallEntry)
                .where(LLMCallEntry.session_id == session_id)
                .order_by(LLMCallEntry.timestamp, LLMCallEntry.id)
            )
            return list(session.scalars(stmt))

    def all_rows(self) -> list[LLMCallEntry]:
        with Session(self._engine) as session:
            stmt = select(LLMCallEntry).order_by(LLMCallEntry.timestamp, LLMCallEntry.id)
            return list(session.scalars(stmt))


def estimate_cost_usd(input_tokens: int, output_tokens: int, config) -> float:
    return (
        input_tokens / 1_000_000 * config.agent_model_input_cost_per_million
        + output_tokens / 1_000_000 * config.agent_model_output_cost_per_million
    )
