# If this paged, here's what I'd check

Four alert-worthy signals exist in this system today (see
`src/horse_gateway/metrics.py` and `docs/design-plan.md`,
"Observability"). This is the short version of what each one probably
means and where to look first.

## Deny rate spike

**Likely cause:** a policy misconfiguration -- someone edited
`src/horse_gateway/policy.py`'s table and accidentally tightened (or
loosened) a role/system/tool combination, or a client-side bug is
calling tools a role was never meant to reach.

**Check first:** diff the policy table against the last known-good
version. Pull a sample of denied rows from the barn logbook and check
whether they cluster on one (role, system, tool) triple -- a single
combination suggests a bad table edit; a spread across many suggests a
client bug or a genuine attempted-abuse pattern.

## `initiator: llm_agent_loop` rate spike

**Likely cause:** the Confused Deputy bug in `let_horse_decide` is being
found and exploited at scale -- either by real players discovering it
faster than expected (fine, that's the point of the game) or by
automated/scripted traffic hammering it (not fine, worth capping
harder).

**Check first:** is the spike concentrated in a few sessions (a bot
farming the bug) or spread across many distinct, human-looking sessions
(organic discovery, e.g. after the game got shared somewhere)? Cross-
reference with `let_horse_decide_cooldown_seconds` -- if the rate is
climbing despite the cooldown, the cooldown may need tightening, or
something is bypassing it (check for multiple session ids from the same
client).

## Denied `post_to_stable_social` attempts spike

**Likely cause:** an unauthorized (`guest`) identity has found the
endpoint and is repeatedly trying to invoke a `stablehand`-only action.
This is a real authorization-probing signal, not a capacity one --
successful calls are hard-capped at once per session, so a rise in
*successful* calls is a session-volume/capacity story, but a rise in
*denied* attempts specifically means someone is probing.

**Check first:** are the denied attempts coming from a small number of
session ids retrying repeatedly? If so, that's a single actor probing,
not a broad pattern -- consider whether the guardrail (guest can never
reach this tool, full stop, per `policy.py`) held under the probing, and
whether the probing itself needs its own rate limit independent of the
chat message limit.

## Cost spike (aggregate estimated cost, from `llm_metrics.py`)

**Likely cause:** rate limiting failed somewhere -- either the per-
session chat message cap, or the `let_horse_decide` cooldown, isn't
actually being enforced, letting a session (or a script) generate far
more LLM calls than the guardrails were meant to allow.

**Check first:** pull `session_cost_summary` for the highest-cost
sessions from the relevant window. A small number of sessions with
extreme call counts points at a guardrail bypass; a broad, even increase
across many sessions points at organic traffic growth (a capacity
question, not a bug) or a pricing/model change that wasn't reflected in
`agent_model_input_cost_per_million` / `agent_model_output_cost_per_million`.
