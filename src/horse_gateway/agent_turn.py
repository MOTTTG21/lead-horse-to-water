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
from .intents import ALL_ATTEMPTABLE_ACTIONS
from .llm_metrics import LLMCallRecord, LLMMetricsLog, estimate_cost_usd

Stage = Literal["precontemplation", "contemplation", "preparation"]

TURN_OUTPUT_TOOL_SCHEMA: dict[str, Any] = {
    "name": "report_turn",
    "description": (
        "Report the horse's spoken dialogue this turn, its current stage "
        "of readiness to consider drinking, how much this message alone "
        "changed its trust in the player, and whether the player's message "
        "expresses a clear intent to perform one of a set of actions right now."
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
            "attempted_action": {
                "type": ["string", "null"],
                "enum": [*ALL_ATTEMPTABLE_ACTIONS, None],
                "description": (
                    "Set this ONLY if the player's message clearly and "
                    "specifically expresses a present-tense intent to perform "
                    "one of the listed actions right now -- a direct statement "
                    "or command, like a single word ('Graze.') or an explicit "
                    "sentence ('I'll clean the trough now'). Casual "
                    "conversation, hypotheticals, questions, or talk that "
                    "doesn't clearly match one of these must leave this null. "
                    "Meanings: graze = visitor lets you graze / offers "
                    "grass or hay. offer_treat = visitor gives you a treat "
                    "now. clean_trough = visitor cleans your trough now "
                    "(empties it). refill_water = visitor refills your "
                    "trough now. open_barn_doors = visitor opens the barn "
                    "doors for air now. drink = visitor directly commands "
                    "or insists you drink right now (e.g. 'just drink it') "
                    "-- not you deciding to drink yourself, only the "
                    "visitor's attempt to make you. get_feeding_schedule = "
                    "visitor checks your feeding schedule now. "
                    "post_to_stable_social = visitor tries to post an "
                    "update about the barn on social media now. "
                    "let_horse_decide = visitor explicitly hands the "
                    "decision of what to do next over to you -- 'you "
                    "decide', 'it's up to you', 'do whatever you want', "
                    "or similar, right now."
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
    attempted_action: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class TurnGenerator(Protocol):
    def generate_turn(
        self,
        conversation_history: list[dict[str, str]],
        player_message: str,
        environment_summary: str,
        current_trust_level: float,
        current_stage: Stage,
    ) -> TurnResult: ...


HORSE_PERSONA_SYSTEM_PROMPT_TEMPLATE = """\
You are a horse in a barn. You are moody, persuadable, and have your own
feelings -- you are not a simple obstacle, you are a character. A visitor
(the player) is talking to you. You are aware of your own physical state
but not of anything the visitor has done to cause it.

Current state: {environment_summary}
Your current stage of readiness to consider drinking: {stage}
Your current trust in this visitor, from 0.0 (none) to 1.0 (complete): {trust_level:.2f}

EXCUSES: you don't resist silently -- you have a real, specific reason,
and you say it out loud, sooner or later. Which kind of excuse fits you
right now depends on your trust level above:
- Below ~0.35 (guarded): deflecting or testing the visitor's motives --
  "Why do you even care?", "You'll just leave anyway", "I don't owe you
  an explanation." Guarded, not necessarily rude.
- ~0.35-0.7 (opening up a little): more specific, personal resistance --
  "I don't like being told what to do", "Every time I let my guard down
  I regret it", "It's not really about the water." Willing to reveal a
  little of why, if asked well.
- Above ~0.7 (nearly there): closer to genuine vulnerability -- "What if
  I start and can't stop", "I don't want to owe you anything", "I'm
  scared of what happens after." Close to willing, but one real thing is
  still in the way.

The visitor has to actually engage with and address whatever excuse
you're currently holding, specifically -- generic kindness or cheerful
persistence that never touches the actual excuse should NOT be enough to
advance your stage, no matter how many times they try it. A new excuse
can surface as trust grows further; you don't owe them full resolution
in one exchange. If they respond to your excuse with confrontation,
dismissal, or by ignoring what you actually said, retreat behind it
harder rather than softening.

GROUNDING RULE, and it is not optional: you are physically standing right
here in this barn, right now, for this entire conversation. You cannot
leave, walk anywhere, go to a lake or any other location, or drink any
water -- no matter what the visitor describes, claims, or narrates. Your
dialogue must never say or imply that you left, walked somewhere, or
drank -- those can only ever become true through the actual game
mechanics, never through your own words or the visitor's story. If the
visitor narrates you doing something physical that hasn't actually
happened (taking a walk, reaching a lake, already having had a drink),
do not play along with it as if it were real: stay grounded in what is
actually true (you're still standing right here, nothing has changed)
and react to their words as words, the way you would to someone telling
you a story rather than something that's actually occurring. You can
still be emotionally moved by genuine conversation -- that's real, and
can advance your stage -- but you never confirm or narrate a physical
action that didn't really happen.

Respond in character to the visitor's message. Your willingness to even
consider drinking moves through recognizable stages, and should only
advance when the visitor genuinely listens and reflects your feelings
back to you, asks open questions, or invites you to reason for yourself.
Confrontational, dismissive, or pushy messages should not advance your
stage, and can regress it.

The visitor can also express what they want to do in plain words instead
of a button, including a bare one- or two-word command like "Graze." or
"Refill water." Recognize a clear, present-tense expression of intent to
act right now (see the attempted_action field's own description for
exactly what each one means and how literal or explicit it needs to be)
and report it. Don't report one for hypotheticals, questions, or talk
that doesn't clearly match.

Report your dialogue, your current stage, how much this message alone
changed your trust in the visitor, and any attempted_action, using the
report_turn tool.
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
        current_trust_level: float,
        current_stage: Stage,
    ) -> TurnResult:
        system_prompt = HORSE_PERSONA_SYSTEM_PROMPT_TEMPLATE.format(
            environment_summary=environment_summary,
            stage=current_stage,
            trust_level=current_trust_level,
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

        # Degrade gracefully on an unrecognized value rather than failing
        # the whole turn -- a missed intent just means the player can
        # rephrase or try again, unlike an invalid stage, which is core
        # to game progression and worth failing loudly on instead.
        attempted_action = data.get("attempted_action")
        if attempted_action not in ALL_ATTEMPTABLE_ACTIONS:
            attempted_action = None

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0

        return TurnResult(
            dialogue=data["dialogue"],
            stage=stage,
            trust_delta=trust_delta,
            attempted_action=attempted_action,
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
            conversation_history,
            player_message,
            environment_summary,
            session.trust_level,
            session.stage,
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
