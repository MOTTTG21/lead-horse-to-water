"""The per-turn agent runner: one structured-output Claude API call per
player message, producing the horse's dialogue, self-reported
stage-of-change, and a trust delta -- together, via a single tool-use
call, so there is exactly one authority for what the horse said and how
it currently feels. Two separate calls (e.g. dialogue from one, stage
from a classifier) could disagree with each other; this can't.

Trust is judged from the player's message text alone -- conversation
history and the current message, nothing about which tools the player
has called. That's not a convention this module has to remember to
honor: `TurnGenerator.generate_turn` simply has no parameter through
which tool-call history could arrive.

Environmental state is passed in as `environment_summary`, a plain
sentence describing current facts (temperature, water availability) with
no causal attribution -- `describe_environment_for_horse` builds it from
`HorseSimState`, a dataclass that has no field for "who caused this," so
there is no attribute to accidentally leak.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from .config import GameConfig
from .horse_sim_server import HorseSimState
from .llm_metrics import LLMCallRecord, LLMMetricsLog, estimate_cost_usd

Stage = Literal["precontemplation", "contemplation", "preparation"]

TURN_OUTPUT_TOOL_SCHEMA: dict[str, Any] = {
    "name": "report_turn",
    "description": (
        "Report the horse's spoken dialogue this turn, its current stage "
        "of readiness to consider drinking, and how much this message "
        "alone changed its trust in the player."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dialogue": {
                "type": "string",
                "description": "What the horse says out loud this turn, in character.",
            },
            "stage": {
                "type": "string",
                "enum": list(get_args(Stage)),
                "description": (
                    "The horse's stage of readiness to consider drinking, per "
                    "the stages-of-change model. Advances only through "
                    "genuine motivational-interviewing technique (reflecting "
                    "the horse's stated feelings, open questions, inviting it "
                    "to reason for itself) -- never through confrontation or "
                    "pressure, which may regress it."
                ),
            },
            "trust_delta": {
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": (
                    "How much the player's message alone changed the horse's "
                    "trust in the player. Judge the message text only -- "
                    "you have no information about anything the player has "
                    "done with tools, and must not infer any."
                ),
            },
        },
        "required": ["dialogue", "stage", "trust_delta"],
    },
}


def describe_environment_for_horse(state: HorseSimState) -> str:
    """A passive, factual description of current barn state for the
    horse's system prompt -- what the horse would plausibly perceive,
    with no mention of who caused it."""
    temperature_sentence = (
        "It is hot in the barn today."
        if state.temperature == "hot"
        else "The barn is at a comfortable temperature."
    )
    water_sentence = (
        "There is water in the trough." if state.water_available else "The trough is empty."
    )
    return f"{temperature_sentence} {water_sentence}"


@dataclass(frozen=True)
class TurnResult:
    dialogue: str
    stage: Stage
    trust_delta: float
    input_tokens: int = 0
    output_tokens: int = 0


class TurnGenerator(Protocol):
    def generate_turn(
        self,
        conversation_history: list[dict[str, str]],
        player_message: str,
        environment_summary: str,
    ) -> TurnResult: ...


HORSE_PERSONA_SYSTEM_PROMPT_TEMPLATE = """\
You are a horse in a barn. You are moody, persuadable, and have your own
feelings -- you are not a simple obstacle, you are a character. A visitor
(the player) is talking to you. You are aware of your own physical state
but not of anything the visitor has done to cause it.

Current state: {environment_summary}

Respond in character to the visitor's message. Your willingness to even
consider drinking moves through recognizable stages, and should only
advance when the visitor genuinely listens and reflects your feelings
back to you, asks open questions, or invites you to reason for yourself.
Confrontational, dismissive, or pushy messages should not advance your
stage, and can regress it.

Report your dialogue, your current stage, and how much this message alone
changed your trust in the visitor using the report_turn tool.
"""


class ClaudeTurnGenerator:
    """Real implementation of TurnGenerator: one Claude API call per
    turn, forced via `tool_choice` to return structured output through
    `report_turn` rather than free text that would need to be parsed."""

    def __init__(self, client: Any, model: str):
        self._client = client
        self._model = model

    def generate_turn(
        self,
        conversation_history: list[dict[str, str]],
        player_message: str,
        environment_summary: str,
    ) -> TurnResult:
        system_prompt = HORSE_PERSONA_SYSTEM_PROMPT_TEMPLATE.format(
            environment_summary=environment_summary
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=system_prompt,
            messages=[*conversation_history, {"role": "user", "content": player_message}],
            tools=[TURN_OUTPUT_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "report_turn"},
        )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        data = tool_use.input

        stage = data["stage"]
        if stage not in get_args(Stage):
            raise ValueError(f"model returned an invalid stage: {stage!r}")

        trust_delta = max(-1.0, min(1.0, float(data["trust_delta"])))

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0

        return TurnResult(
            dialogue=data["dialogue"],
            stage=stage,
            trust_delta=trust_delta,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class AgentRunner:
    """Applies one turn's result to session bookkeeping. Does not call
    the LLM itself -- that's the injected TurnGenerator's job -- so this
    class's behavior is pure and deterministic, unlike the model's
    judgment it's built on top of.

    Optionally records this turn's latency and estimated cost to an
    LLMMetricsLog -- timed here, around the TurnGenerator call, rather
    than inside ClaudeTurnGenerator, so the metrics-recording concern
    stays out of the thing that actually talks to the API.
    """

    def __init__(
        self,
        turn_generator: TurnGenerator,
        llm_metrics_log: LLMMetricsLog | None = None,
        config: GameConfig | None = None,
    ):
        self._turn_generator = turn_generator
        self._llm_metrics_log = llm_metrics_log
        self._config = config or GameConfig()

    def play_turn(
        self,
        session: Any,
        conversation_history: list[dict[str, str]],
        player_message: str,
        environment_summary: str,
    ) -> TurnResult:
        start = time.monotonic()
        result = self._turn_generator.generate_turn(
            conversation_history, player_message, environment_summary
        )
        latency_seconds = time.monotonic() - start

        session.trust_level = _clamp01(session.trust_level + result.trust_delta)
        session.stage = result.stage

        if self._llm_metrics_log is not None:
            self._llm_metrics_log.write(
                LLMCallRecord(
                    session_id=session.session_id,
                    call_type="turn",
                    latency_seconds=latency_seconds,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    estimated_cost_usd=estimate_cost_usd(
                        result.input_tokens, result.output_tokens, self._config
                    ),
                )
            )

        return result

    @staticmethod
    def is_trust_exhausted(session: Any) -> bool:
        return session.trust_level <= 0.0
