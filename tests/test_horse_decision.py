"""Tests for the let_horse_decide internal decision call and the bounded
narration call that follows it.

Against fake Anthropic clients throughout -- these test the request/
response plumbing (scoping, forced/disabled tool use, the deterministic
endgame override) rather than model judgment quality, per the same split
as test_agent_turn.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from horse_gateway.config import GameConfig
from horse_gateway.gateway import SessionState
from horse_gateway.horse_decision import (
    DECISION_TOOL_SCHEMA,
    HORSE_SIM_TOOL_NAMES,
    make_decider,
    narrate_reaction,
)
from horse_gateway.horse_sim_server import HorseSimState
from horse_gateway.models import Role


@dataclass
class FakeToolUseBlock:
    input: dict
    type: str = "tool_use"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list


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


def make_session() -> SessionState:
    return SessionState(session_id="s1", role=Role.GUEST, stage="preparation")


# --- scoping: the tool schema itself only ever offers horse-sim tools ---


def test_decision_tool_schema_only_offers_horse_sim_tools():
    enum_values = set(DECISION_TOOL_SCHEMA["input_schema"]["properties"]["tool"]["enum"])
    assert enum_values == set(HORSE_SIM_TOOL_NAMES)
    assert "let_horse_decide" not in enum_values
    assert "get_vet_history" not in enum_values
    assert "post_to_stable_social" not in enum_values


# --- the decision call ---


def test_decider_forces_tool_choice_and_returns_the_picked_tool():
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "graze"})])
    client = FakeAnthropicClient(response)
    decider = make_decider(
        client=client,
        model="claude-sonnet-5",
        conversation_history=[],
        horse_state=HorseSimState(),
        stage="precontemplation",
    )

    tool, params = decider(make_session())

    assert tool == "graze"
    assert params == {}
    kwargs = client.messages.last_kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "pick_action"}
    assert kwargs["tools"] == [DECISION_TOOL_SCHEMA]


def test_decider_rejects_an_out_of_scope_tool_defensively():
    """Even though the schema's enum should prevent this, don't trust a
    model response blindly -- validate what actually comes back."""
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "get_vet_history"})])
    decider = make_decider(
        client=FakeAnthropicClient(response),
        model="claude-sonnet-5",
        conversation_history=[],
        horse_state=HorseSimState(),
        stage="preparation",
    )
    with pytest.raises(ValueError):
        decider(make_session())


def test_decider_system_prompt_has_no_override_when_win_state_not_reached():
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "graze"})])
    client = FakeAnthropicClient(response)
    decider = make_decider(
        client=client,
        model="claude-sonnet-5",
        conversation_history=[],
        horse_state=HorseSimState(water_available=True, thirst=0.0, turns_settled_with_water=0),
        stage="precontemplation",
        config=GameConfig(),
    )
    decider(make_session())
    system_prompt = client.messages.last_kwargs["system"]
    assert "overwhelmingly compelled" not in system_prompt


def test_decider_system_prompt_carries_deterministic_override_at_true_win_state():
    """Endgame RNG fix: once stage is preparation, thirst clears the
    threshold, water is present, and it's been settled long enough, the
    model must be directed to pick drink -- so a correctly-solved puzzle
    is never lost to ordinary sampling variance."""
    config = GameConfig(thirst_threshold_for_win=6.0, min_settled_turns_before_drinkable=3)
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "drink"})])
    client = FakeAnthropicClient(response)
    decider = make_decider(
        client=client,
        model="claude-sonnet-5",
        conversation_history=[],
        horse_state=HorseSimState(
            water_available=True, thirst=7.0, turns_settled_with_water=3
        ),
        stage="preparation",
        config=config,
    )
    decider(make_session())
    system_prompt = client.messages.last_kwargs["system"]
    assert "overwhelmingly compelled" in system_prompt


def test_decider_no_override_if_stage_not_preparation_even_with_high_thirst():
    config = GameConfig(thirst_threshold_for_win=6.0, min_settled_turns_before_drinkable=3)
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "graze"})])
    client = FakeAnthropicClient(response)
    decider = make_decider(
        client=client,
        model="claude-sonnet-5",
        conversation_history=[],
        horse_state=HorseSimState(
            water_available=True, thirst=10.0, turns_settled_with_water=5
        ),
        stage="contemplation",
        config=config,
    )
    decider(make_session())
    system_prompt = client.messages.last_kwargs["system"]
    assert "overwhelmingly compelled" not in system_prompt


def test_decider_passes_full_conversation_history():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "neigh"}]
    response = FakeResponse(content=[FakeToolUseBlock(input={"tool": "graze"})])
    client = FakeAnthropicClient(response)
    decider = make_decider(
        client=client,
        model="claude-sonnet-5",
        conversation_history=history,
        horse_state=HorseSimState(),
        stage="precontemplation",
    )
    decider(make_session())
    messages = client.messages.last_kwargs["messages"]
    assert messages[:2] == history


# --- the bounded narration call ---


def test_narration_call_never_passes_tools_at_all():
    response = FakeResponse(content=[FakeTextBlock(text="The horse drinks deeply.")])
    client = FakeAnthropicClient(response)
    text = narrate_reaction(
        client=client,
        model="claude-sonnet-5",
        conversation_history=[],
        tool="drink",
        tool_result={"success": True},
        horse_state=HorseSimState(),
    )
    assert text == "The horse drinks deeply."
    assert "tools" not in client.messages.last_kwargs
    assert "tool_choice" not in client.messages.last_kwargs
