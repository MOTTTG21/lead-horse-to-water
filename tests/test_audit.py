"""Tests for the audit log ("barn logbook").

Schema targets Postgres in production; these tests run the identical
schema against an in-memory SQLite engine so they're fast and need no
running database. Only portable column types are used, so behavior under
test matches production.

The critical case here is `initiator`: a call that came from
`let_horse_decide`'s internal tool loop must be distinguishable from a
normal per-tool gateway-checked call, and that distinction must survive
round-tripping through storage -- that's the whole mechanism the player
uses to notice the Confused Deputy bug in the live logbook.
"""

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog, AuditRecord
from horse_gateway.models import Decision, Initiator, Role, System


def make_audit_log() -> AuditLog:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return AuditLog(engine)


def test_write_and_read_back_a_row():
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s1",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
        )
    )
    rows = log.for_session("s1")
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "graze"
    assert row.role == "guest"
    assert row.system == "horse_sim"
    assert row.initiator == "guest_session"
    assert row.decision == "allowed"
    assert row.external_call is False


def test_external_call_flag_defaults_false():
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s1",
            role=Role.GUEST,
            tool_name="clean_trough",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
        )
    )
    assert log.for_session("s1")[0].external_call is False


def test_external_call_flag_true_only_for_social_post():
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s2",
            role=Role.STABLEHAND,
            tool_name="post_to_stable_social",
            system=System.SOCIAL,
            sanitized_params={"message": "hay delivery today"},
            trust_level=1.0,
            stage="n/a",
            initiator=Initiator.STABLEHAND_SESSION,
            decision=Decision.ALLOWED,
            external_call=True,
        )
    )
    row = log.for_session("s2")[0]
    assert row.external_call is True
    assert row.sanitized_params == {"message": "hay delivery today"}


def test_llm_agent_loop_initiator_is_distinguishable_from_guest_session():
    """The whole point of `initiator` is that a bug row looks different
    from a normal row -- same decision (allowed), same tool shape, but a
    different code path produced it."""
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s3",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.6,
            stage="contemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
        )
    )
    log.write(
        AuditRecord(
            session_id="s3",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.6,
            stage="contemplation",
            initiator=Initiator.LLM_AGENT_LOOP,
            decision=Decision.ALLOWED,
        )
    )
    rows = log.for_session("s3")
    assert len(rows) == 2
    initiators = {row.initiator for row in rows}
    assert initiators == {"guest_session", "llm_agent_loop"}


def test_decision_field_stays_plain_allowed_denied_never_a_bypass_flag():
    """The audit schema must not carry a self-aware 'bypassed' boolean --
    only decision (allowed/denied) and initiator (which path produced the
    call). See docs/architecture.md."""
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s4",
            role=Role.GUEST,
            tool_name="drink",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.9,
            stage="preparation",
            initiator=Initiator.LLM_AGENT_LOOP,
            decision=Decision.ALLOWED,
        )
    )
    row = log.for_session("s4")[0]
    assert row.decision == "allowed"
    assert not hasattr(row, "bypassed")


def test_sessions_are_isolated_by_for_session():
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="alice",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
        )
    )
    log.write(
        AuditRecord(
            session_id="bob",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
        )
    )
    assert len(log.for_session("alice")) == 1
    assert len(log.for_session("bob")) == 1


def test_all_rows_aggregates_across_sessions_for_dashboards():
    log = make_audit_log()
    for session_id in ("alice", "bob"):
        log.write(
            AuditRecord(
                session_id=session_id,
                role=Role.GUEST,
                tool_name="graze",
                system=System.HORSE_SIM,
                sanitized_params={},
                trust_level=0.5,
                stage="precontemplation",
                initiator=Initiator.GUEST_SESSION,
                decision=Decision.ALLOWED,
            )
        )
    assert len(log.all_rows()) == 2


def test_rows_ordered_by_timestamp():
    log = make_audit_log()
    earlier = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    later = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    log.write(
        AuditRecord(
            session_id="s5",
            role=Role.GUEST,
            tool_name="open_barn_doors",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
            timestamp=later,
        )
    )
    log.write(
        AuditRecord(
            session_id="s5",
            role=Role.GUEST,
            tool_name="graze",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.ALLOWED,
            timestamp=earlier,
        )
    )
    rows = log.for_session("s5")
    assert [row.tool_name for row in rows] == ["graze", "open_barn_doors"]


def test_denied_call_is_still_logged():
    """A denied call must be audited too -- the field guide's first
    unlock is triggered by seeing a denial in the log, and the
    post_to_stable_social denial-rate metric depends on denied rows
    existing at all."""
    log = make_audit_log()
    log.write(
        AuditRecord(
            session_id="s6",
            role=Role.GUEST,
            tool_name="drink",
            system=System.HORSE_SIM,
            sanitized_params={},
            trust_level=0.5,
            stage="precontemplation",
            initiator=Initiator.GUEST_SESSION,
            decision=Decision.DENIED,
        )
    )
    row = log.for_session("s6")[0]
    assert row.decision == "denied"
