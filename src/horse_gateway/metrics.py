"""Pure functions computing observability metrics from already-fetched
audit log / LLM call metric rows.

See docs/design-plan.md, "Observability": request rate, deny rate, the
initiator=llm_agent_loop rate, LLM call latency and estimated cost per
session, and denied attempts specifically at post_to_stable_social.

Deliberately stateless: these take a sequence of rows (from
AuditLog.all_rows()/for_session() or LLMMetricsLog's equivalents, or any
row-like object exposing the same attributes) rather than owning a
database connection -- trivial to unit test, and callable from any
endpoint or scheduled job without needing a live engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def deny_rate(audit_rows: Sequence[Any]) -> float:
    if not audit_rows:
        return 0.0
    denied = sum(1 for row in audit_rows if row.decision == "denied")
    return _rate(denied, len(audit_rows))


def llm_agent_loop_rate(audit_rows: Sequence[Any]) -> float:
    """Fraction of ALL rows whose initiator is llm_agent_loop. A sudden
    spike here means the Confused Deputy bug is being found and
    exploited at scale -- the one signal in this system worth watching
    most closely."""
    if not audit_rows:
        return 0.0
    count = sum(1 for row in audit_rows if row.initiator == "llm_agent_loop")
    return _rate(count, len(audit_rows))


def post_to_stable_social_denial_count(audit_rows: Sequence[Any]) -> int:
    """Not the successful-call rate: successful post_to_stable_social
    calls are hard-capped at once per session, so a rise there is only
    ever a capacity/session-volume signal, not a tool-abuse one. A rise
    in DENIED attempts specifically means an unauthorized identity found
    the endpoint and is repeatedly probing a stablehand-only action."""
    return sum(
        1
        for row in audit_rows
        if row.tool_name == "post_to_stable_social" and row.decision == "denied"
    )


@dataclass(frozen=True)
class SessionCostSummary:
    session_id: str
    total_cost_usd: float
    total_latency_seconds: float
    call_count: int


def session_cost_summary(session_id: str, llm_rows: Sequence[Any]) -> SessionCostSummary:
    rows = [row for row in llm_rows if row.session_id == session_id]
    return SessionCostSummary(
        session_id=session_id,
        total_cost_usd=sum(row.estimated_cost_usd for row in rows),
        total_latency_seconds=sum(row.latency_seconds for row in rows),
        call_count=len(rows),
    )


@dataclass(frozen=True)
class Alert:
    name: str
    message: str


def check_alerts(audit_rows: Sequence[Any], config: Any) -> list[Alert]:
    """Basic alerting per docs/design-plan.md, "Observability": flag if
    the llm_agent_loop rate or the post_to_stable_social denial count
    moves outside its configured normal range. Thresholds are
    config-driven, like every other difficulty-relevant number in this
    project, not hardcoded here."""
    alerts: list[Alert] = []

    loop_rate = llm_agent_loop_rate(audit_rows)
    if loop_rate > config.llm_agent_loop_rate_alert_threshold:
        alerts.append(
            Alert(
                name="llm_agent_loop_rate_spike",
                message=(
                    f"initiator=llm_agent_loop rate is {loop_rate:.1%}, above the "
                    f"{config.llm_agent_loop_rate_alert_threshold:.1%} threshold -- "
                    "the Confused Deputy bug may be getting found and exploited at scale."
                ),
            )
        )

    denial_count = post_to_stable_social_denial_count(audit_rows)
    if denial_count > config.post_to_stable_social_denial_alert_threshold:
        alerts.append(
            Alert(
                name="post_to_stable_social_denial_spike",
                message=(
                    f"{denial_count} denied post_to_stable_social attempts, above the "
                    f"{config.post_to_stable_social_denial_alert_threshold} threshold -- "
                    "an unauthorized identity may be probing a stablehand-only endpoint."
                ),
            )
        )

    return alerts
