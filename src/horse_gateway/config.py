"""Config-driven thresholds and guardrail values.

Per the design doc: "All difficulty-relevant thresholds ... are config
values from the start, not hardcoded constants." The numbers below are
placeholder defaults pending soft-launch playtest data (see the Open
Questions section of docs/design-plan.md) -- the point of this module is
that retuning them later is a config change, not a code change. Override
any field via an environment variable prefixed `HORSE_GAME_` (e.g.
`HORSE_GAME_LET_HORSE_DECIDE_COOLDOWN_SECONDS=45`).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class GameConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HORSE_GAME_")

    # Which Claude model powers the horse's dialogue, stage/trust
    # judgment, and internal tool-picking decision.
    agent_model: str = "claude-sonnet-5"

    # Cost estimation for the observability layer. Claude Sonnet 5
    # pricing as of 2026-09; update if pricing or agent_model changes.
    agent_model_input_cost_per_million: float = 2.0
    agent_model_output_cost_per_million: float = 10.0

    # Basic alerting thresholds (see docs/design-plan.md, "Observability").
    llm_agent_loop_rate_alert_threshold: float = 0.05
    post_to_stable_social_denial_alert_threshold: int = 5

    # Guardrails (see docs/design-plan.md "Guardrails" section).
    let_horse_decide_cooldown_seconds: float = 45.0
    social_post_cap_per_session: int = 1
    # Hard cap on chat messages per session, in a rolling window -- "no
    # unauthenticated [or authenticated] endpoint that can trigger
    # unbounded LLM calls."
    chat_rate_limit_max_messages: int = 20
    chat_rate_limit_window_seconds: float = 600.0
    # Global (all-sessions) daily spend ceiling on the Claude API budget.
    # Once crossed, /turn and /let-horse-decide fail gracefully instead
    # of making another LLM call, until the next UTC day.
    daily_spend_cap_usd: float = 20.0

    # Live horse simulation thresholds (see "Model call architecture" and
    # "Refill timing" in docs/design-plan.md). Thirst is an unbounded
    # accrual counter, not a fixed 0-10 scale -- there is no ceiling to
    # clamp against, only a threshold past which the horse is "thirsty
    # enough." The real difficulty lever is min_settled_turns_before_drinkable
    # and the persona's excuses (agent_turn.py) -- thirst itself is trivial
    # to clear by repeating clean_trough/open_barn_doors, on purpose (the
    # design doc treats environmental setup as an ordering puzzle, not the
    # hard part; genuine conversation is meant to be the hard part).
    thirst_increment_per_dry_action: float = 2.0
    thirst_increment_per_turn_while_dry: float = 0.5
    thirst_increment_per_turn_while_hot: float = 0.5
    thirst_threshold_for_win: float = 6.0
    min_settled_turns_before_drinkable: int = 5

    # Synthetic stablehand traffic (see "Roles" in docs/design-plan.md).
    synthetic_traffic_interval_seconds: float = 15.0
    # Probability of rotating to a fresh synthetic tenant after a
    # non-social action, so the logbook shows more than one long-lived
    # synthetic session over time. A social post always rotates
    # regardless of this value, once its once-per-session cap is used.
    synthetic_traffic_new_tenant_probability: float = 0.05


DEFAULT_CONFIG = GameConfig()
