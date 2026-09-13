"""Chat-level guardrails: a per-session message rate limit and a global
daily spend cap.

Neither belongs in Gateway -- Gateway only knows about tool calls, and
these two guard the chat turn itself (the /turn and /let-horse-decide
LLM calls), which happen regardless of whether any tool gets touched.
Per docs/design-plan.md, "Guardrails": both are meant to be built "from
day one, not phase 2," alongside the let_horse_decide cooldown and the
post_to_stable_social cap that already live in gateway.py.
"""

from __future__ import annotations

import datetime as dt

from ..config import GameConfig
from ..llm_metrics import LLMMetricsLog
from .session_store import PlayerSession


def is_rate_limited(
    session: PlayerSession, config: GameConfig, now: dt.datetime | None = None
) -> bool:
    """True if this session has already sent chat_rate_limit_max_messages
    within the rolling chat_rate_limit_window_seconds window. Prunes
    expired timestamps as a side effect, so callers don't need to."""
    now = now or dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(seconds=config.chat_rate_limit_window_seconds)
    session.message_timestamps = [t for t in session.message_timestamps if t >= window_start]
    return len(session.message_timestamps) >= config.chat_rate_limit_max_messages


def record_message(session: PlayerSession, now: dt.datetime | None = None) -> None:
    session.message_timestamps.append(now or dt.datetime.now(dt.timezone.utc))


def daily_spend_usd(llm_metrics_log: LLMMetricsLog, today: dt.date | None = None) -> float:
    """Total estimated LLM cost across ALL sessions for one UTC calendar
    day. Deliberately global, not per-session -- a per-session cap alone
    is trivial to bypass by clearing cookies for a fresh session; this is
    the real backstop against a cost blowout."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    rows = llm_metrics_log.all_rows()
    return sum(row.estimated_cost_usd for row in rows if row.timestamp.date() == today)
