"""Tests for the LLM call metrics store: latency, token counts, and
estimated cost per Claude API call, distinct from the audit log (which
records tool-call authorization, not LLM call cost)."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.config import GameConfig
from horse_gateway.llm_metrics import LLMCallRecord, LLMMetricsLog, estimate_cost_usd


def make_log() -> LLMMetricsLog:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return LLMMetricsLog(engine)


def test_write_and_read_back_a_row():
    log = make_log()
    log.write(
        LLMCallRecord(
            session_id="s1",
            call_type="turn",
            latency_seconds=0.42,
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.001,
        )
    )
    rows = log.for_session("s1")
    assert len(rows) == 1
    row = rows[0]
    assert row.call_type == "turn"
    assert row.latency_seconds == 0.42
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.estimated_cost_usd == 0.001


def test_sessions_isolated_by_for_session():
    log = make_log()
    log.write(
        LLMCallRecord(
            session_id="a",
            call_type="decision",
            latency_seconds=0.1,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.0001,
        )
    )
    log.write(
        LLMCallRecord(
            session_id="b",
            call_type="narration",
            latency_seconds=0.2,
            input_tokens=20,
            output_tokens=10,
            estimated_cost_usd=0.0002,
        )
    )
    assert len(log.for_session("a")) == 1
    assert len(log.for_session("b")) == 1
    assert len(log.all_rows()) == 2


def test_estimate_cost_usd_uses_config_pricing():
    config = GameConfig(
        agent_model_input_cost_per_million=2.0, agent_model_output_cost_per_million=10.0
    )
    cost = estimate_cost_usd(input_tokens=1_000_000, output_tokens=1_000_000, config=config)
    assert cost == 12.0


def test_estimate_cost_usd_zero_tokens_is_zero_cost():
    config = GameConfig()
    assert estimate_cost_usd(0, 0, config) == 0.0
