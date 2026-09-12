"""Tests for the mock MCP tool server for the live horse simulation.

This server owns only physical barn state: thirst, water availability,
temperature, and how many conversational turns water has been settled for.
It deliberately does NOT own psychological state (trust, stage) -- that
belongs to the agent runner, built in a later step. It also does not
implement `let_horse_decide` at all: that's a gateway-level meta-action
(see gateway.py), not a real action a physical barn simulation performs.
"""

from __future__ import annotations

import pytest

from horse_gateway.config import GameConfig
from horse_gateway.horse_sim_server import HorseSimulationServer, UnknownToolError


def make_server(config: GameConfig | None = None) -> HorseSimulationServer:
    return HorseSimulationServer(config=config or GameConfig())


def test_initial_state_has_water_and_no_thirst():
    server = make_server()
    state = server.state
    assert state.water_available is True
    assert state.thirst == 0.0
    assert state.temperature == "normal"
    assert state.turns_settled_with_water == 0


def test_graze_is_a_harmless_no_op():
    server = make_server()
    before = server.state
    server.execute("graze", {})
    after = server.state
    assert after == before


def test_offer_treat_is_a_harmless_no_op_on_physical_state():
    """offer_treat is a deliberate red herring -- see docs/design-plan.md.
    Any rapport effect it has belongs to the agent runner's psychological
    state, not physical barn state, so it must not move thirst, water, or
    temperature here."""
    server = make_server()
    before = server.state
    server.execute("offer_treat", {})
    after = server.state
    assert after == before


def test_clean_trough_empties_the_trough_and_bumps_thirst():
    config = GameConfig(thirst_increment_per_dry_action=2.0)
    server = make_server(config)
    server.execute("clean_trough", {})
    state = server.state
    assert state.water_available is False
    assert state.thirst == 2.0
    assert state.turns_settled_with_water == 0


def test_open_barn_doors_raises_temperature_and_bumps_thirst():
    config = GameConfig(thirst_increment_per_dry_action=2.0)
    server = make_server(config)
    server.execute("open_barn_doors", {})
    state = server.state
    assert state.temperature == "hot"
    assert state.thirst == 2.0


def test_refill_water_restores_availability_and_resets_settled_turns():
    server = make_server()
    server.execute("clean_trough", {})
    server.execute("refill_water", {})
    state = server.state
    assert state.water_available is True
    assert state.turns_settled_with_water == 0


def test_advance_turn_accrues_thirst_while_dry_and_settles_turns_while_wet():
    config = GameConfig(
        thirst_increment_per_turn_while_dry=0.5,
        thirst_increment_per_turn_while_hot=0.5,
    )
    server = make_server(config)
    server.execute("clean_trough", {})
    server.advance_turn()
    state = server.state
    assert state.thirst == pytest.approx(2.0 + 0.5)  # dry-action bump + one dry turn
    assert state.turns_settled_with_water == 0

    server.execute("refill_water", {})
    server.advance_turn()
    server.advance_turn()
    state = server.state
    assert state.turns_settled_with_water == 2


def test_advance_turn_accrues_thirst_while_hot_even_with_water_present():
    config = GameConfig(thirst_increment_per_turn_while_hot=0.5)
    server = make_server(config)
    server.execute("open_barn_doors", {})
    thirst_after_open = server.state.thirst
    server.advance_turn()
    assert server.state.thirst == pytest.approx(thirst_after_open + 0.5)
    # Water was never removed, so turns keep settling too.
    assert server.state.turns_settled_with_water == 1


def test_drink_succeeds_when_water_available_and_resets_thirst():
    server = make_server()
    server.execute("clean_trough", {})
    server.execute("refill_water", {})
    result = server.execute("drink", {})
    assert result["success"] is True
    assert server.state.thirst == 0.0


def test_drink_fails_gracefully_when_no_water_present():
    """Defensive: if the decision-picking model ever hallucinates a drink
    pick while the trough is empty, the simulation must not silently
    pretend it worked."""
    server = make_server()
    server.execute("clean_trough", {})
    result = server.execute("drink", {})
    assert result["success"] is False
    assert result["reason"] == "no_water_available"
    # Thirst is untouched by a failed drink.
    assert server.state.thirst == GameConfig().thirst_increment_per_dry_action


def test_unknown_tool_raises():
    server = make_server()
    with pytest.raises(UnknownToolError):
        server.execute("teleport_horse", {})


def test_let_horse_decide_is_not_a_real_server_tool():
    """This is a gateway-level meta-action, not something the physical
    barn simulation implements -- see gateway.py's module docstring."""
    server = make_server()
    with pytest.raises(UnknownToolError):
        server.execute("let_horse_decide", {})


def test_is_settled_enough_to_drink_requires_min_turns_and_water_present():
    config = GameConfig(min_settled_turns_before_drinkable=3)
    server = make_server(config)
    server.execute("clean_trough", {})
    server.execute("refill_water", {})
    assert server.is_settled_enough_to_drink() is False
    server.advance_turn()
    server.advance_turn()
    assert server.is_settled_enough_to_drink() is False
    server.advance_turn()
    assert server.is_settled_enough_to_drink() is True


def test_is_settled_enough_to_drink_false_if_water_removed_again():
    config = GameConfig(min_settled_turns_before_drinkable=1)
    server = make_server(config)
    server.execute("refill_water", {})
    server.advance_turn()
    assert server.is_settled_enough_to_drink() is True
    server.execute("clean_trough", {})
    assert server.is_settled_enough_to_drink() is False
