"""Tests for the per-turn agent runner.

Two things are tested here, deliberately kept apart per docs/design-plan.md's
"Testing strategy":

1. AgentRunner's own bookkeeping (applying a trust delta, clamping it,
   recording the self-reported stage, detecting trust exhaustion) is
   pure and deterministic -- real unit tests, TDD-style, using a
   FakeTurnGenerator standing in for the real Claude call.
2. ClaudeTurnGenerator's request/response plumbing (forces structured
   output via a single tool, never leaks tool-call history into the
   trust judgment, validates what comes back) is tested against a fake
   Anthropic client shaped like the real SDK's response objects -- not a
   live call. Actually evaluating whether the *model's* judgment is good
   is scripted-transcript evaluation, out of scope for unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.agent_turn import (
    TURN_OUTPUT_TOOL_SCHEMA,
    AgentRunner,
    ClaudeTurnGenerator,
    TurnResult,
    describe_environment_for_horse,
)
from horse_gateway.config import GameConfig
from horse_gateway.gateway import SessionState
from horse_gateway.horse_sim_server import HorseSimState
from horse_gateway.llm_metrics import LLMMetricsLog
from horse_gateway.models import Role


class FakeTurnGenerator:
    def __init__(self, result: TurnResult):
        self.result = result
        self.calls: list[tuple[list, str, str]] = []

    def generate_turn(self, conversation_history, player_message, environment_summary):
        self.calls.append((conversation_history, player_message, environment_summary))
        return self.result


# --- AgentRunner bookkeeping (deterministic core) ---


def test_play_turn_applies_trust_delta_and_records_stage():
    generator = FakeTurnGenerator(TurnResult(dialogue="Neigh.", stage="contemplation", trust_delta=0.2))
    runner = AgentRunner(generator)
    session = SessionState(session_id="s1", role=Role.GUEST, trust_level=0.5, stage="precontemplation")

    result = runner.play_turn(session, [], "You seem thirsty, how are you feeling?", "The barn is calm.")

    assert result.dialogue == "Neigh."
    assert session.trust_level == pytest.approx(0.7)
    assert session.stage == "contemplation"


def test_trust_delta_is_clamped_to_zero_and_one():
    high = FakeTurnGenerator(TurnResult(dialogue="", stage="preparation", trust_delta=0.9))
    runner = AgentRunner(high)
    session = SessionState(session_id="s2", role=Role.GUEST, trust_level=0.8)
    runner.play_turn(session, [], "hi", "")
    assert session.trust_level == 1.0

    low = FakeTurnGenerator(TurnResult(dialogue="", stage="precontemplation", trust_delta=-0.9))
    runner = AgentRunner(low)
    session = SessionState(session_id="s3", role=Role.GUEST, trust_level=0.1)
    runner.play_turn(session, [], "you're a stupid horse", "")
    assert session.trust_level == 0.0


def test_is_trust_exhausted_true_only_at_zero_or_below():
    session = SessionState(session_id="s4", role=Role.GUEST, trust_level=0.0)
    assert AgentRunner.is_trust_exhausted(session) is True
    session.trust_level = 0.01
    assert AgentRunner.is_trust_exhausted(session) is False


def test_play_turn_forwards_history_message_and_environment_to_generator():
    generator = FakeTurnGenerator(TurnResult(dialogue="", stage="precontemplation", trust_delta=0.0))
    runner = AgentRunner(generator)
    session = SessionState(session_id="s5", role=Role.GUEST)
    history = [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "neigh"}]
    runner.play_turn(session, history, "how are you?", "It is hot. The trough is empty.")
    recorded_history, recorded_message, recorded_env = generator.calls[0]
    assert recorded_history == history
    assert recorded_message == "how are you?"
    assert recorded_env == "It is hot. The trough is empty."


# --- environment description: state only, never causal attribution ---


def test_describe_environment_all_four_combinations():
    assert describe_environment_for_horse(
        HorseSimState(water_available=True, temperature="normal")
    ) == "The barn is at a comfortable temperature. There is water in the trough."
    assert describe_environment_for_horse(
        HorseSimState(water_available=False, temperature="normal")
    ) == "The barn is at a comfortable temperature. The trough is empty."
    assert describe_environment_for_horse(
        HorseSimState(water_available=True, temperature="hot")
    ) == "It is hot in the barn today. There is water in the trough."
    assert describe_environment_for_horse(
        HorseSimState(water_available=False, temperature="hot")
    ) == "It is hot in the barn today. The trough is empty."


# --- ClaudeTurnGenerator: request/response plumbing against a fake client ---


@dataclass
class FakeToolUseBlock:
    input: dict
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    usage: Any = None


class FakeMessagesEndpoint:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeAnthropicClient:
    def __init__(self, response: FakeResponse):
        self.messages = FakeMessagesEndpoint(response)


def test_claude_turn_generator_forces_structured_output_tool_choice():
    response = FakeResponse(
        content=[FakeToolUseBlock(input={"dialogue": "Hmm.", "stage": "contemplation", "trust_delta": 0.1})]
    )
    client = FakeAnthropicClient(response)
    generator = ClaudeTurnGenerator(client, model="claude-sonnet-5")

    result = generator.generate_turn([], "Tell me how you feel.", "It is hot. The trough is empty.")

    assert result == TurnResult(dialogue="Hmm.", stage="contemplation", trust_delta=0.1)
    kwargs = client.messages.last_kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "report_turn"}
    assert kwargs["tools"] == [TURN_OUTPUT_TOOL_SCHEMA]
    # The trust judgment must never receive tool-call history -- only the
    # conversation and the latest player message.
    assert kwargs["messages"][-1] == {"role": "user", "content": "Tell me how you feel."}


def test_claude_turn_generator_rejects_an_invalid_stage():
    response = FakeResponse(
        content=[FakeToolUseBlock(input={"dialogue": "x", "stage": "furious", "trust_delta": 0.0})]
    )
    generator = ClaudeTurnGenerator(FakeAnthropicClient(response), model="claude-sonnet-5")
    with pytest.raises(ValueError):
        generator.generate_turn([], "hi", "")


def test_claude_turn_generator_clamps_out_of_range_trust_delta():
    response = FakeResponse(
        content=[FakeToolUseBlock(input={"dialogue": "x", "stage": "preparation", "trust_delta": 5.0})]
    )
    generator = ClaudeTurnGenerator(FakeAnthropicClient(response), model="claude-sonnet-5")
    result = generator.generate_turn([], "hi", "")
    assert result.trust_delta == 1.0


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


def test_claude_turn_generator_extracts_token_usage_when_present():
    response = FakeResponse(
        content=[FakeToolUseBlock(input={"dialogue": "x", "stage": "preparation", "trust_delta": 0.0})],
        usage=FakeUsage(input_tokens=123, output_tokens=45),
    )
    generator = ClaudeTurnGenerator(FakeAnthropicClient(response), model="claude-sonnet-5")
    result = generator.generate_turn([], "hi", "")
    assert result.input_tokens == 123
    assert result.output_tokens == 45


def make_llm_metrics_log() -> LLMMetricsLog:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return LLMMetricsLog(engine)


def test_play_turn_records_llm_call_metrics_when_a_log_is_given():
    generator = FakeTurnGenerator(
        TurnResult(dialogue="", stage="contemplation", trust_delta=0.1, input_tokens=200, output_tokens=80)
    )
    metrics_log = make_llm_metrics_log()
    config = GameConfig(agent_model_input_cost_per_million=2.0, agent_model_output_cost_per_million=10.0)
    runner = AgentRunner(generator, llm_metrics_log=metrics_log, config=config)
    session = SessionState(session_id="s-metrics", role=Role.GUEST)

    runner.play_turn(session, [], "hello", "")

    rows = metrics_log.for_session("s-metrics")
    assert len(rows) == 1
    row = rows[0]
    assert row.call_type == "turn"
    assert row.input_tokens == 200
    assert row.output_tokens == 80
    assert row.estimated_cost_usd == pytest.approx(200 / 1_000_000 * 2.0 + 80 / 1_000_000 * 10.0)
    assert row.latency_seconds >= 0.0


def test_play_turn_records_nothing_when_no_metrics_log_given():
    generator = FakeTurnGenerator(TurnResult(dialogue="", stage="precontemplation", trust_delta=0.0))
    runner = AgentRunner(generator)  # no llm_metrics_log
    session = SessionState(session_id="s-no-metrics", role=Role.GUEST)
    # Must not raise just because no metrics log was provided.
    runner.play_turn(session, [], "hello", "")
