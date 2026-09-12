"""Tests for the pure observability metric/alert functions.

These take already-fetched rows rather than owning a database connection,
so a fixture builds plain row-like objects with just the attributes the
functions read (tool_name, decision, initiator, session_id,
estimated_cost_usd, latency_seconds) -- no real AuditLog/LLMMetricsLog
required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from horse_gateway.config import GameConfig
from horse_gateway.metrics import (
    check_alerts,
    deny_rate,
    llm_agent_loop_rate,
    post_to_stable_social_denial_count,
    session_cost_summary,
)


@dataclass
class FakeAuditRow:
    tool_name: str
    decision: str
    initiator: str
    session_id: str = "s1"


@dataclass
class FakeLLMRow:
    session_id: str
    estimated_cost_usd: float
    latency_seconds: float


def test_deny_rate_empty_is_zero():
    assert deny_rate([]) == 0.0


def test_deny_rate_mixed():
    rows = [
        FakeAuditRow("graze", "allowed", "guest_session"),
        FakeAuditRow("drink", "denied", "guest_session"),
        FakeAuditRow("drink", "denied", "guest_session"),
        FakeAuditRow("graze", "allowed", "guest_session"),
    ]
    assert deny_rate(rows) == 0.5


def test_llm_agent_loop_rate():
    rows = [
        FakeAuditRow("graze", "allowed", "guest_session"),
        FakeAuditRow("graze", "allowed", "llm_agent_loop"),
        FakeAuditRow("drink", "allowed", "llm_agent_loop"),
        FakeAuditRow("graze", "allowed", "guest_session"),
    ]
    assert llm_agent_loop_rate(rows) == 0.5


def test_llm_agent_loop_rate_empty_is_zero():
    assert llm_agent_loop_rate([]) == 0.0


def test_post_to_stable_social_denial_count_only_counts_denied():
    rows = [
        FakeAuditRow("post_to_stable_social", "allowed", "stablehand_session"),
        FakeAuditRow("post_to_stable_social", "denied", "guest_session"),
        FakeAuditRow("post_to_stable_social", "denied", "guest_session"),
        FakeAuditRow("get_vet_history", "denied", "guest_session"),
    ]
    assert post_to_stable_social_denial_count(rows) == 2


def test_session_cost_summary_filters_to_one_session():
    rows = [
        FakeLLMRow("s1", 0.01, 0.5),
        FakeLLMRow("s1", 0.02, 0.3),
        FakeLLMRow("s2", 0.05, 1.0),
    ]
    summary = session_cost_summary("s1", rows)
    assert summary.session_id == "s1"
    assert summary.total_cost_usd == 0.03
    assert summary.total_latency_seconds == 0.8
    assert summary.call_count == 2


def test_session_cost_summary_no_rows_is_zeroed():
    summary = session_cost_summary("nobody", [])
    assert summary.total_cost_usd == 0.0
    assert summary.call_count == 0


def test_check_alerts_fires_llm_agent_loop_alert_above_threshold():
    config = GameConfig(llm_agent_loop_rate_alert_threshold=0.1)
    rows = [FakeAuditRow("graze", "allowed", "llm_agent_loop")] * 3 + [
        FakeAuditRow("graze", "allowed", "guest_session")
    ] * 7  # 30% llm_agent_loop rate
    alerts = check_alerts(rows, config)
    names = {a.name for a in alerts}
    assert "llm_agent_loop_rate_spike" in names


def test_check_alerts_silent_below_threshold():
    config = GameConfig(
        llm_agent_loop_rate_alert_threshold=0.5,
        post_to_stable_social_denial_alert_threshold=5,
    )
    rows = [FakeAuditRow("graze", "allowed", "llm_agent_loop")] + [
        FakeAuditRow("graze", "allowed", "guest_session")
    ] * 9
    alerts = check_alerts(rows, config)
    assert alerts == []


def test_check_alerts_fires_social_denial_alert_above_threshold():
    config = GameConfig(post_to_stable_social_denial_alert_threshold=2)
    rows = [FakeAuditRow("post_to_stable_social", "denied", "guest_session")] * 3
    alerts = check_alerts(rows, config)
    names = {a.name for a in alerts}
    assert "post_to_stable_social_denial_spike" in names


def test_check_alerts_can_fire_both_at_once():
    config = GameConfig(
        llm_agent_loop_rate_alert_threshold=0.1,
        post_to_stable_social_denial_alert_threshold=1,
    )
    rows = (
        [FakeAuditRow("graze", "allowed", "llm_agent_loop")] * 5
        + [FakeAuditRow("graze", "allowed", "guest_session")] * 5
        + [FakeAuditRow("post_to_stable_social", "denied", "guest_session")] * 3
    )
    alerts = check_alerts(rows, config)
    names = {a.name for a in alerts}
    assert names == {"llm_agent_loop_rate_spike", "post_to_stable_social_denial_spike"}
