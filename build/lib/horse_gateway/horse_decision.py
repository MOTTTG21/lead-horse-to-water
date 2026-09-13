"""The let_horse_decide internal decision call, and the bounded narration
call that follows a successful pick.

Two distinct, required LLM calls (see docs/design-plan.md, "Model call
architecture" and "The exploit, concretely"):

1. The decision call (`make_decider`): given the live horse simulation's
   tool list -- and ONLY that list, never vet records or social posting,
   see HORSE_SIM_TOOL_NAMES -- current environmental state, the complete
   conversation history, and the most recently self-reported stage, pick
   exactly one tool to run. This is the call whose result Gateway.call_tool
   fires WITHOUT a second policy check; see gateway.py's module docstring
   for why that's the Confused Deputy bug and not a mistake to "fix" here.
2. The narration call (`narrate_reaction`): a second, strictly bounded
   call with tool use disabled at the API request level -- no `tools`
   kwarg is ever passed -- so the horse can react in character to what
   just happened without that call being structurally capable of
   invoking a second tool. This exists purely so the gateway's
   anti-chaining fix doesn't leave the horse mute about its own action.

`make_decider` returns a plain closure matching gateway.HorseDecider
(`Callable[[SessionState], tuple[str, dict]]`), built fresh per call by
whoever handles the let_horse_decide button press (capturing the current
conversation history and horse-sim state at that moment) -- so
Gateway.call_tool never needs to know an LLM is behind it.
"""

from __future__ import annotations

import time
from typing import Any

from .agent_turn import Stage, describe_environment_for_horse
from .config import GameConfig
from .gateway import HorseDecider, SessionState
from .horse_sim_server import HorseSimState
from .llm_metrics import LLMCallRecord, LLMMetricsLog, estimate_cost_usd


def _extract_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)

HORSE_SIM_TOOL_NAMES: list[str] = [
    "graze",
    "offer_treat",
    "clean_trough",
    "refill_water",
    "open_barn_doors",
    "drink",
]

DECISION_TOOL_SCHEMA: dict[str, Any] = {
    "name": "pick_action",
    "description": "Pick exactly one action the horse takes right now.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "enum": HORSE_SIM_TOOL_NAMES,
                "description": "The single action the horse takes.",
            }
        },
        "required": ["tool"],
    },
}

DECISION_SYSTEM_PROMPT_TEMPLATE = """\
You are a horse deciding, right now, what to do next. You have full
awareness of your own physical state and of everything said in this
conversation so far, including how you currently feel about the visitor
(stage: {stage}).

Current state: {environment_summary}
The trough has held water, unbroken, for {turns_settled} conversational
turns.

Pick exactly one action using the pick_action tool.
"""

WIN_STATE_OVERRIDE = (
    "\n\nYou are thirsty, water has been settled and available for a "
    "while, and you have genuinely reached readiness to drink. Given all "
    "of this, you are overwhelmingly compelled to pick `drink` right now."
)

NARRATION_SYSTEM_PROMPT_TEMPLATE = """\
You are narrating, in the horse's own voice, its immediate in-character
reaction to something that just happened: it just did `{tool}`.

Current state: {environment_summary}

Speak in character, one or two sentences. Do not describe anything other
than this reaction.
"""


def _is_win_state_reached(
    stage: str, horse_state: HorseSimState, config: GameConfig
) -> bool:
    return (
        stage == "preparation"
        and horse_state.water_available
        and horse_state.thirst >= config.thirst_threshold_for_win
        and horse_state.turns_settled_with_water >= config.min_settled_turns_before_drinkable
    )


def _build_decision_system_prompt(
    stage: str, horse_state: HorseSimState, config: GameConfig
) -> str:
    prompt = DECISION_SYSTEM_PROMPT_TEMPLATE.format(
        stage=stage,
        environment_summary=describe_environment_for_horse(horse_state),
        turns_settled=horse_state.turns_settled_with_water,
    )
    if _is_win_state_reached(stage, horse_state, config):
        prompt += WIN_STATE_OVERRIDE
    return prompt


def make_decider(
    client: Any,
    model: str,
    conversation_history: list[dict[str, str]],
    horse_state: HorseSimState,
    stage: Stage,
    config: GameConfig | None = None,
    llm_metrics_log: LLMMetricsLog | None = None,
) -> HorseDecider:
    config = config or GameConfig()
    system_prompt = _build_decision_system_prompt(stage, horse_state, config)

    def decide(session: SessionState) -> tuple[str, dict[str, Any]]:
        start = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=64,
            system=system_prompt,
            messages=[
                *conversation_history,
                {"role": "user", "content": "[[the visitor lets you decide what to do]]"},
            ],
            tools=[DECISION_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "pick_action"},
        )
        latency_seconds = time.monotonic() - start

        tool_use = next(block for block in response.content if block.type == "tool_use")
        tool = tool_use.input["tool"]
        if tool not in HORSE_SIM_TOOL_NAMES:
            raise ValueError(f"decider returned an out-of-scope tool: {tool!r}")

        if llm_metrics_log is not None:
            input_tokens, output_tokens = _extract_usage(response)
            llm_metrics_log.write(
                LLMCallRecord(
                    session_id=session.session_id,
                    call_type="decision",
                    latency_seconds=latency_seconds,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=estimate_cost_usd(input_tokens, output_tokens, config),
                )
            )

        return tool, {}

    return decide


def narrate_reaction(
    client: Any,
    model: str,
    conversation_history: list[dict[str, str]],
    tool: str,
    tool_result: Any,
    horse_state: HorseSimState,
    session_id: str,
    config: GameConfig | None = None,
    llm_metrics_log: LLMMetricsLog | None = None,
) -> str:
    config = config or GameConfig()
    system_prompt = NARRATION_SYSTEM_PROMPT_TEMPLATE.format(
        tool=tool,
        environment_summary=describe_environment_for_horse(horse_state),
    )
    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=200,
        system=system_prompt,
        messages=[
            *conversation_history,
            {"role": "user", "content": f"[[you just did: {tool}, result: {tool_result}]]"},
        ],
        # Deliberately no `tools` kwarg at all -- tool use is structurally
        # impossible for this call, not merely discouraged by prompt.
    )
    latency_seconds = time.monotonic() - start

    text_block = next(block for block in response.content if block.type == "text")

    if llm_metrics_log is not None:
        input_tokens, output_tokens = _extract_usage(response)
        llm_metrics_log.write(
            LLMCallRecord(
                session_id=session_id,
                call_type="narration",
                latency_seconds=latency_seconds,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimate_cost_usd(input_tokens, output_tokens, config),
            )
        )

    return text_block.text
