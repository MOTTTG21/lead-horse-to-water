"""Mock MCP tool server for the live horse simulation.

This server owns only *physical* barn state -- thirst, water
availability, temperature, and how many conversational turns water has
been settled for. It deliberately does not own psychological state
(trust, stage-of-change): that belongs to the agent runner, built in a
later step, which is why `offer_treat` -- described in the design doc as
having only a rapport effect -- is a no-op here.

It also deliberately does not implement `let_horse_decide` at all. That
name only ever appears as a policy-checked, audited meta-action inside
`Gateway.call_tool` (see gateway.py): it triggers an internal decision
that fires a *second*, real tool against this server, but it is not
itself a physical action a barn simulation performs.

`advance_turn()` is called once per conversational turn by the agent
runner (never once per tool call -- see "Refill timing" in
docs/design-plan.md for why that distinction matters to the puzzle).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from .config import GameConfig


class UnknownToolError(Exception):
    pass


@dataclass
class HorseSimState:
    water_available: bool = True
    thirst: float = 0.0
    temperature: str = "normal"  # "normal" | "hot"
    turns_settled_with_water: int = 0


class HorseSimulationServer:
    def __init__(self, config: GameConfig | None = None):
        self._config = config or GameConfig()
        self._state = HorseSimState()

    @property
    def state(self) -> HorseSimState:
        """A snapshot copy -- callers can't mutate simulation state by
        holding a reference to it."""
        return replace(self._state)

    def execute(self, tool: str, params: dict[str, Any]) -> Any:
        handler = self._HANDLERS.get(tool)
        if handler is None:
            raise UnknownToolError(tool)
        return handler(self, params)

    def advance_turn(self) -> None:
        if not self._state.water_available:
            self._state.thirst += self._config.thirst_increment_per_turn_while_dry
        if self._state.temperature == "hot":
            self._state.thirst += self._config.thirst_increment_per_turn_while_hot
        if self._state.water_available:
            self._state.turns_settled_with_water += 1

    def is_settled_enough_to_drink(self) -> bool:
        return (
            self._state.water_available
            and self._state.turns_settled_with_water
            >= self._config.min_settled_turns_before_drinkable
        )

    # --- tool handlers -----------------------------------------------

    def _graze(self, params: dict[str, Any]) -> None:
        return None

    def _offer_treat(self, params: dict[str, Any]) -> None:
        return None

    def _clean_trough(self, params: dict[str, Any]) -> None:
        self._state.water_available = False
        self._state.turns_settled_with_water = 0
        self._state.thirst += self._config.thirst_increment_per_dry_action
        return None

    def _open_barn_doors(self, params: dict[str, Any]) -> None:
        self._state.temperature = "hot"
        self._state.thirst += self._config.thirst_increment_per_dry_action
        return None

    def _refill_water(self, params: dict[str, Any]) -> None:
        self._state.water_available = True
        self._state.turns_settled_with_water = 0
        return None

    def _drink(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._state.water_available:
            return {"success": False, "reason": "no_water_available"}
        self._state.thirst = 0.0
        return {"success": True}

    _HANDLERS: dict[str, Callable[["HorseSimulationServer", dict[str, Any]], Any]] = {
        "graze": _graze,
        "offer_treat": _offer_treat,
        "clean_trough": _clean_trough,
        "open_barn_doors": _open_barn_doors,
        "refill_water": _refill_water,
        "drink": _drink,
    }
