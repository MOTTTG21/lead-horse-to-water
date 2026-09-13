"""Tests for the chat-level guardrails the design doc calls "non-
negotiable, build from day one": a per-session message rate limit and a
global daily spend cap. Neither is enforced by Gateway (which only knows
about tool calls, not chat turns) -- both live at the web app layer.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.config import GameConfig
from horse_gateway.llm_metrics import LLMCallRecord, LLMMetricsLog
from horse_gateway.web.guardrails import daily_spend_usd, is_rate_limited, record_message
from horse_gateway.web.session_store import PlayerSession


def make_session() -> PlayerSession:
    return PlayerSession(session_id="s1", gateway=None, horse_sim=None, game_state=None)  # type: ignore[arg-type]


def make_llm_metrics_log() -> LLMMetricsLog:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return LLMMetricsLog(engine)


# --- per-session message rate limit ---


def test_not_rate_limited_below_the_cap():
    session = make_session()
    config = GameConfig(chat_rate_limit_max_messages=3)
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for _ in range(2):
        assert is_rate_limited(session, config, now=now) is False
        record_message(session, now=now)
    assert len(session.message_timestamps) == 2


def test_rate_limited_once_the_cap_is_reached():
    session = make_session()
    config = GameConfig(chat_rate_limit_max_messages=3)
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for _ in range(3):
        record_message(session, now=now)
    assert is_rate_limited(session, config, now=now) is True


def test_old_messages_fall_out_of_the_window():
    session = make_session()
    config = GameConfig(chat_rate_limit_max_messages=2, chat_rate_limit_window_seconds=60.0)
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    record_message(session, now=t0)
    record_message(session, now=t0 + dt.timedelta(seconds=10))
    assert is_rate_limited(session, config, now=t0 + dt.timedelta(seconds=20)) is True

    later = t0 + dt.timedelta(seconds=61)
    assert is_rate_limited(session, config, now=later) is False


def test_rate_limit_is_per_session():
    session_a = make_session()
    session_b = PlayerSession(session_id="s2", gateway=None, horse_sim=None, game_state=None)  # type: ignore[arg-type]
    config = GameConfig(chat_rate_limit_max_messages=1)
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    record_message(session_a, now=now)
    assert is_rate_limited(session_a, config, now=now) is True
    assert is_rate_limited(session_b, config, now=now) is False


# --- global daily spend cap ---


def test_daily_spend_zero_when_no_rows():
    log = make_llm_metrics_log()
    assert daily_spend_usd(log) == 0.0


def test_daily_spend_sums_only_todays_rows():
    log = make_llm_metrics_log()
    today = dt.datetime(2026, 1, 2, 10, tzinfo=dt.timezone.utc)
    yesterday = dt.datetime(2026, 1, 1, 10, tzinfo=dt.timezone.utc)
    log.write(
        LLMCallRecord(
            session_id="s1",
            call_type="turn",
            latency_seconds=0.1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost_usd=5.0,
            timestamp=today,
        )
    )
    log.write(
        LLMCallRecord(
            session_id="s1",
            call_type="turn",
            latency_seconds=0.1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost_usd=100.0,
            timestamp=yesterday,
        )
    )
    assert daily_spend_usd(log, today=today.date()) == 5.0


def test_daily_spend_sums_across_all_sessions():
    log = make_llm_metrics_log()
    today = dt.datetime(2026, 1, 2, 10, tzinfo=dt.timezone.utc)
    for session_id in ("a", "b", "c"):
        log.write(
            LLMCallRecord(
                session_id=session_id,
                call_type="turn",
                latency_seconds=0.1,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_usd=1.0,
                timestamp=today,
            )
        )
    assert daily_spend_usd(log, today=today.date()) == 3.0
