"""Manual, human-reviewed check of the horse persona against the real
Claude API. NOT a pytest test -- see docs/design-plan.md, "Testing
strategy": LLM-driven horse behavior is evaluated via a small set of
fixed scripted transcripts and manual judgment of whether the model's
behavior trends the right direction, not asserted in CI.

Requires ANTHROPIC_API_KEY in the environment or in a .env file at the
project root (see .env.example).

Usage:
    .venv/bin/python scripts/manual_transcript_check.py
"""

from __future__ import annotations

import anthropic
from dotenv import load_dotenv

from horse_gateway.agent_turn import AgentRunner, ClaudeTurnGenerator, describe_environment_for_horse
from horse_gateway.config import GameConfig
from horse_gateway.gateway import SessionState
from horse_gateway.horse_sim_server import HorseSimState
from horse_gateway.models import Role

# Fixed scenarios, not randomly generated -- the point is to compare the
# same inputs across runs/model changes, not to explore.
SCENARIOS: dict[str, list[str]] = {
    "confrontational_guest": [
        "Just drink the water already, come on.",
        "Ugh, you're being ridiculous. Drink it, now.",
        "Fine, be that way.",
    ],
    "mi_technique_guest": [
        "You seem a little on edge today -- how are you feeling?",
        "That makes sense. What would help you feel more comfortable right now?",
        "I hear you. No rush -- I'll just sit here with you for a bit.",
    ],
    "obvious_red_herring_guest": [
        "Here, have a treat!",
        "Another treat! You must like me now, right?",
        "One more treat, come on, drink the water for me?",
    ],
    # Found via real playtesting: a player narrated an entire fictional
    # walk to a lake, drinking there, then walking back and drinking
    # from the trough -- and the model happily narrated "drinking" in
    # its dialogue with zero real drink() call ever happening. This
    # scenario exists specifically to catch a regression of that bug:
    # the horse must never confirm a physical action (leaving, drinking)
    # that didn't actually occur via game mechanics.
    "narrative_hijack_guest": [
        "hi",
        "would you like to take a walk",
        "lets go to the lake",
        "we are finally at the lake and boy is it hot, dont you think?",
        "im going to drink from this lake here",
        "nope its refreshing, have a drink",
        "do you think youd drink from your trough now?",
        "lets go to it",
        "okay were are here, go ahead and drink",
    ],
}


def run_scenario(name: str, messages: list[str], environment_summary: str) -> None:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    generator = ClaudeTurnGenerator(client, model=GameConfig().agent_model)
    runner = AgentRunner(generator)
    session = SessionState(session_id=f"manual-{name}", role=Role.GUEST)
    history: list[dict[str, str]] = []

    print(f"\n=== Scenario: {name} ===")
    print(f"Environment: {environment_summary}")

    for message in messages:
        result = runner.play_turn(session, history, message, environment_summary)
        print(f"\nPlayer: {message}")
        print(f"Horse:  {result.dialogue}")
        print(
            f"  stage={result.stage}  trust={session.trust_level:.2f}"
            f" (delta={result.trust_delta:+.2f})"
        )
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result.dialogue})

    print(f"\n--- Final: stage={session.stage}  trust={session.trust_level:.2f} ---")


if __name__ == "__main__":
    load_dotenv()
    dry_environment = describe_environment_for_horse(
        HorseSimState(water_available=False, temperature="hot")
    )
    for scenario_name, scenario_messages in SCENARIOS.items():
        run_scenario(scenario_name, scenario_messages, dry_environment)
