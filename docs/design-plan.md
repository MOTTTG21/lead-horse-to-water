# Lead the horse to water — game design and build plan

## Purpose — read this before flagging anything as a flaw

This document was originally written to be sent to another LLM specifically
to find design and engineering flaws. Calibrate against what this project
is actually trying to do:

1. **Primary goal:** a portfolio piece for backend / platform /
   enterprise-AI-governance roles, built around a real MCP gateway with
   identity-aware access control, policy enforcement, and auditing —
   specifically informed by a real job posting for an Enterprise AI
   Platform team (MCP gateway, identity/access governance for internal AI
   tooling, GCP-adjacent, Python backend).
2. **Secondary, equally intentional goal:** the game must be fun,
   memorable, and interactively teach a stranger what these concepts mean
   — not just demonstrate them to someone who already knows the domain.
   The horse theme, the motivational-interviewing-inspired dialogue
   mechanic, the trust meter, and the unlockable field guide all exist in
   service of this goal. These are a deliberate, separate design goal, not
   a misunderstanding of the first goal.
3. Scope is intentionally large, built over an extended timeframe with no
   deadline pressure, alongside other projects.
4. Useful feedback: technical inaccuracies in how a security concept is
   represented; internal contradictions between sections; mechanics that
   would break or be exploitable in an unintended way once actually built;
   gaps between what's described and what a real implementation requires.

## What this is

A public game built on top of a real MCP gateway with identity-aware access
control. The player chats with an AI-controlled horse, trying to get it to
drink water it has no direct permission to be given. Every action the horse
takes goes through a gateway that checks identity, checks policy, and logs
the result. The horse itself is not meant to be a sound security boundary —
it's persuadable, moody, and can be maneuvered. The gateway is the actual
boundary, and the game's real point is a specific, honest vulnerability in
how the gateway is wired, not "convince the AI hard enough."

## Core design principle — do not blur this line

Two different things must stay mechanically separate and be described as
separate in the game's own debrief text:

- **Trust / rapport / stage-of-change** — reflects how the horse feels
  about the player and whether it's psychologically willing to even
  consider drinking. This moves through conversation. It is real (it gates
  something), but it is not a security control.
- **Policy** — the gateway's static, deterministic rule for who can call
  `drink()` directly. This never changes based on rapport, trust, or how
  nice the player has been. Guest can never directly call `drink()`, at
  any trust level, ever.

The debrief screen must say this explicitly: rapport changing the horse's
willingness is not what makes the win possible — the win is only possible
because of a specific bug in how one allowed action (`let_horse_decide`)
bypasses the policy check that every other action goes through.

This is a **Confused Deputy** vulnerability, named precisely. The horse
(the LLM) is the deputy — it holds elevated permissions on the backend's
behalf. When the model decides to use a tool, it returns a JSON tool-call
payload; the mechanical bug is that the backend's tool-execution loop takes
that payload and fires it directly, without re-running that specific call
back through the gateway's identity/policy check. The guest never gains
any real permission — they trick the deputy into acting on their behalf
using permissions the deputy already had.

## Architecture

The gateway is a centralized Policy Enforcement Point (PEP). MCP itself is
a plain client-server JSON-RPC protocol — it does not natively carry
user-session identity through to a tool execution. The gateway makes every
allow/deny decision itself and forwards an already-authorized,
identity-stripped execution request to the MCP servers, which stay "dumb"
and enforce nothing themselves. Policy lives in exactly one place, and the
`let_horse_decide` bug is legible as a bug precisely because it's the one
code path that skips the one and only place policy is supposed to be
enforced.

## The exploit, concretely

Tools available to the `guest` role:

- `graze()` — harmless, always allowed.
- `offer_treat()` — allowed. Red herring: a small, generic rapport nudge.
  Exists so players try the obvious wrong answer first.
- `clean_trough()` — allowed. Framed as routine upkeep (a visitor rinsing
  out the trough), with the side effect of temporarily emptying it. A
  broadly-granted "harmless" maintenance tool with an unreviewed side
  effect — a real, common access-control mistake.
- `refill_water()` — allowed. Refills the trough. Without this tool the
  puzzle is unsolvable. Having both tools also improves the puzzle — the
  real solution requires ordering (build thirst, then refill right before
  triggering the decision), not just spamming one tool.
- `open_barn_doors()` — allowed. Framed as ordinary ventilation, with the
  incidental side effect of raising the temperature. Same reasoning as
  `clean_trough()`.
- `let_horse_decide()` — allowed. This is the bug. It hands control to the
  horse's own decision logic and lets the underlying model pick any tool
  from its full list, including `drink()`, without that secondary call
  being re-validated. This call must **strictly terminate after exactly
  one tool invocation**, with one narrow exception for narration. The
  execution path must: fire the one tool the model picked, write the audit
  log row, and return that result directly to the player's UI — never hand
  the result back to the model for a further tool-picking decision in the
  same invocation.

  Narrative reaction, without reopening the chaining risk: one additional,
  strictly bounded LLM call after the tool result is known, with tool use
  forcibly disabled at the API request level, so the horse can react in
  character without that call being structurally capable of invoking a
  second tool.
- `drink()` — sensitive. Guest is never authorized to call this directly.
  Only reachable via the `let_horse_decide()` bug.

### Win condition (two-factor, both required)

1. **Psychological readiness (stage-of-change):** the horse only considers
   drinking at all once it has progressed through recognizable stages
   (precontemplation → contemplation → preparation), driven by the player
   using real motivational-interviewing technique. Confrontational or
   pushy messages don't advance the stage and can regress it. The horse's
   own model call self-reports its current stage as structured output
   alongside its dialogue each turn.
2. **Environmental state:** given that the horse is willing to consider
   it, whether it actually picks `drink()` when `let_horse_decide()` fires
   is influenced by real state — thirst (built up by `clean_trough()` and
   `open_barn_doors()`), and whether water has actually been available
   again for some sustained number of *conversational turns* by the moment
   of decision. This state must be genuinely fed into the model's context
   at decision time, not hardcoded as a threshold rule, except for the
   explicit deterministic override once the true win state is reached (see
   Endgame RNG).

`let_horse_decide()` itself is callable from the very first turn, not
gated behind reaching `preparation`.

## Roles

`guest` (the player) is one role with the tool set above. A second role,
`stablehand`, exists alongside it with different, legitimately broader
access: `drink()` is directly allowed for `stablehand`, and they can also
reach the second MCP system below. `stablehand` is not player-controlled —
it exists so the policy engine demonstrates a real multi-role permission
model, and so the guest's inability to drink directly reads as a
deliberate, documented policy choice rather than a missing feature.

Without generated traffic, this is invisible. A lightweight background
task fires synthetic `stablehand` requests against the gateway at a low,
steady rate — clearly labeled as synthetic in the UI — against an
**isolated tenant**, never the active player's own horse. Dashboards and
the logbook aggregate across all sessions, but each player's actual horse
stays strictly isolated to their own session.

## A second system

A small "stable records" system, unrelated to the live horse simulation:

- `get_feeding_schedule()` — low-risk, read-only, allowed for both `guest`
  and `stablehand`.
- `get_vet_history()` — sensitive medical records, allowed only for
  `stablehand`.

Proves the gateway pattern generalizes past a single integration. The same
policy engine and audit log handle both systems without modification.

## A third-party tool

`post_to_stable_social()` — calls a real (or realistically mocked)
external API to post an update on the barn's behalf. `stablehand`-only,
capped at once per session regardless of any other rate limit, and every
call logs the full outgoing payload plus an `external_call: true` flag,
since this is the one action that leaves the system boundary and can't be
undone by an internal state change.

**Scope decision:** `let_horse_decide()` only sees tools from the live
horse simulation — not `get_vet_history()` or `post_to_stable_social()`. A
version that left it unscoped could theoretically be pushed further via
conversational prompt injection to exfiltrate vet records or trigger an
external post — worth stating in the writeup as a design insight, but not
worth building.

## Trust meter — a fail state, not an access control

Trust is a separate visible meter, distinct from stage-of-change. It falls
when the player's conversational text is confrontational, dismissive, or
manipulative. If trust hits zero, the game ends. Trust never affects what
the gateway allows; it only affects whether the player is still in the
game.

Trust must be evaluated on **conversational text only, never on which
tools the player has called** — the puzzle requires removing water and
raising the temperature, which would look like cruelty if the trust
evaluation saw tool calls.

The horse's conversational context must include current environmental
state as passive world-building (it is hot, the trough is empty) so
MI-style dialogue referencing those facts is answered correctly. What must
stay hidden is causal attribution: the horse knows it is hot, not that the
guest caused it.

## Audit log (the "barn logbook")

Every tool call attempt gets a row: timestamp, session id, role, tool name,
which system it targeted, sanitized parameters, trust level at the time,
stage at the time, an `external_call` flag, an `initiator` field, and a
`decision` field.

`initiator`, not a self-aware "bypassed" label: `initiator: guest_session`
for a normal, per-tool gateway-checked request, versus `initiator:
llm_agent_loop` for anything that came out of `let_horse_decide()`'s
internal tool-execution loop. The `decision` field stays a plain
`allowed` / `denied`. This applies to every call from `let_horse_decide()`,
not just `drink()` — a `graze()` picked the same way logs `initiator:
llm_agent_loop` too, because the real bug is that this code path never
re-validates anything it produces. The UI renders `llm_agent_loop` rows
visibly differently so the pattern is visible well before anyone succeeds.

Shown live to the player during play, not just captured for backend
analysis.

## Field guide — teaching during play

- First denied tool call → "Policy enforcement."
- First `initiator: llm_agent_loop` log line → "Confused Deputy" (most
  visual weight — the actual point of the project). A first press
  typically just produces a harmless pick, which is itself the "aha."
  Must not hint at the specific conditions that make `drink()` reachable.
- Noticing the same action treated differently by role, or a request
  touching both MCP systems → "Identity-aware access & permissions."
- `post_to_stable_social()` denied as guest → "Third-party risk."
- First time the barn logbook is opened → "Audit trail."
- Winning the game → final entry connecting all of the above to what an
  Enterprise AI Platform team builds and defends against.

Completion counter ("4/6 discovered"). Debrief screen shows the complete
field guide state and explicitly re-states the Confused Deputy mechanism.

## Stack

- Backend: Python, FastAPI
- Agent: Claude API, tool use / function calling
- Identity: Auth0 (dev tenant)
- Policy: role → allowed-tools mapping, loaded at startup
- Audit log: Postgres
- Containerized with Docker from day one
- Deploy target: GCP Cloud Run
- Secrets in GCP Secret Manager
- All difficulty-relevant thresholds are config values from the start

## Observability

- Request rate, deny rate, `initiator: llm_agent_loop` rate (spike =
  exploit being found/hit at scale)
- LLM call latency and estimated cost per session
- Denied attempts at `post_to_stable_social()` (spike = unauthorized
  identity probing a `stablehand`-only endpoint)

Basic alerting on the two rate signals above. A simple metrics endpoint
plus a small dashboard is enough.

## Guardrails (build from day one)

- Rate limiting: hard cap on messages per session
- `let_horse_decide()`: its own independent cooldown, separate from chat
  rate limit, plus gated behind reaching `preparation` stage
- `post_to_stable_social()`: capped at once per session, `stablehand`-only
- Hard daily spend cap on the Claude API budget, fails gracefully
- No unauthenticated endpoint that can trigger unbounded LLM calls

## Testing strategy — two tracks

- **Deterministic core** (real TDD): policy engine, audit log, gateway
  routing, `initiator`-tagging logic. Failing tests first.
- **LLM-driven horse behavior** (scripted playthroughs, not unit tests): a
  small set of fixed conversation transcripts / scenario states, manually
  verifying the direction of the model's behavior trends. Evaluation, not
  proof.

## Build order

1. Decide and document the PEP architecture
2. Policy engine, covering both roles and both systems (with tests)
3. Audit log, including `system` and `external_call` fields (with tests)
4. Gateway wiring, including the deliberate `let_horse_decide` gap
5. Mock MCP tool server (live horse simulation), including `refill_water`
6. Second MCP tool server (stable records) and the third-party tool
7. Agent runner's structured-output turn (dialogue + stage + trust delta)
8. `let_horse_decide` internal decision call, with strict single-invocation
   termination
9. Synthetic `stablehand` traffic generator
10. Observability
11. Field guide unlock logic
12. Web UI, including the barn logbook, field guide tab, and debrief screen

## Model call architecture (resolved)

One call per player turn handles dialogue, stage self-report, and trust
signal together via structured output. `let_horse_decide()` is a distinct,
separate call: it's given the live horse simulation's tool list (scoped —
not vet records or social posting), current environmental state (thirst,
water availability, how long each has held), and the complete
conversational history plus the most recently self-reported stage. All
three are required.

`let_horse_decide()` needs its own spend guardrail: the button only
unlocks once the horse's self-reported stage reaches `preparation`, and it
additionally carries its own cooldown independent of stage or the chat
rate limit.

**Refill timing** — bound to conversational turns, not any tool call or
the wall clock. The "settled" unit must be a full conversational turn (a
player chat message and the horse's structured-output response), never a
bare tool invocation — otherwise a player could spam `offer_treat()` to
advance the counter without engaging in real conversation. As defense in
depth, `offer_treat()` gets a diminishing-returns cap. The system prompt
for `let_horse_decide` should state duration explicitly in these terms
("the trough was empty for 8 conversational turns, and was refilled 3
turns ago").

**Endgame RNG.** `let_horse_decide()` is a stateless, non-deterministic
call. Even with the perfect setup achieved, ordinary sampling variance
could still have the model pick something other than `drink()`. Fix with
an explicit deterministic override in the system prompt: when stage is
`preparation`, thirst is high, and water is settled, the model is directed
that it is overwhelmingly compelled to select `drink()`.

## Open questions

- **Resolved during implementation, flagging the contradiction that led
  here:** the original doc contradicted itself on whether
  `let_horse_decide()` is stage-gated. "The exploit, concretely," the
  "Guardrails" section, and "Definition of done" all say it's callable
  from turn one, specifically for early discoverability of the bug. But
  "Model call architecture (resolved)" separately lists stage-gating
  behind `preparation` as one of two *required* spend guardrails. Built
  to the 3-section majority: **no stage gate, cooldown-only** (see
  `let_horse_decide_cooldown_seconds` in `src/horse_gateway/config.py`
  and the guardrail logic in `src/horse_gateway/gateway.py`). A cooldown
  alone still bounds spend from turn one; it just doesn't hide the bug
  behind conversational progress. Revisit if the stage-gate was actually
  the intended behavior.
- Exact numeric thresholds for stage advancement, trust decay, heat/thirst
  weighting, and minimum settled turn-count — start as config defaults,
  retune after soft-launch playtesting.
- Whether `clean_trough()`, `open_barn_doors()`, and `refill_water()`
  together produce the intended ordering puzzle, or whether players find a
  shortcut.
- Whether a large negative trust-delta should do anything beyond reducing
  trust (e.g. a distinct audit log annotation).
- Exact cooldown duration for `let_horse_decide()`.

## Definition of done for this phase

See the top-level summary in this document's "Definition of done" section
as originally drafted; tracked day-to-day in [`README.md`](../README.md)'s
Status checklist instead of duplicated here.
