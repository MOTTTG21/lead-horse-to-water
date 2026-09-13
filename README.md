# Lead the horse to water

A public game built on top of a real MCP gateway with identity-aware access
control. The player chats with an AI-controlled horse, trying to get it to
drink water it has no direct permission to be given. Every action the horse
takes goes through a gateway that checks identity, checks policy, and logs
the result.

The horse itself is not a security boundary — it's persuadable, moody, and
can be maneuvered. The gateway is the real boundary, and the point of the
game is a specific, honest vulnerability in how the gateway is wired: a
[Confused Deputy](docs/architecture.md) bug in one allowed action
(`let_horse_decide`) that skips the policy check every other action goes
through.

See [`docs/architecture.md`](docs/architecture.md) for the Policy
Enforcement Point design decision and the exact shape of the bug.

## Status

Following the build order (smallest, most isolated pieces first):

- [x] 1. PEP architecture documented (`docs/architecture.md`)
- [x] 2. Policy engine, both roles + both systems (`src/horse_gateway/policy.py`, tested)
- [x] 3. Audit log, `system` + `external_call` fields (`src/horse_gateway/audit.py`, tested)
- [x] 4. Gateway wiring, including the deliberate `let_horse_decide` gap (`src/horse_gateway/gateway.py`, tested)
- [x] 5. Mock MCP tool server (live horse simulation) (`src/horse_gateway/horse_sim_server.py`, tested)
- [x] 6. Second MCP tool server (stable records) + third-party tool (`stable_records_server.py`, `social_server.py`, tested, plus an end-to-end multi-system integration test)
- [x] 7. Agent runner's structured-output turn (dialogue + stage + trust delta) (`agent_turn.py`, tested against a fake Anthropic client)
- [x] 8. `let_horse_decide` internal decision call + bounded narration call (`horse_decision.py`, tested)
- [x] 9. Synthetic `stablehand` traffic generator (`synthetic_traffic.py`, tested)
- [x] 10. Observability -- metrics/alerting library (`llm_metrics.py`, `metrics.py`, tested; runbook at [`docs/runbook.md`](docs/runbook.md)). The actual HTTP metrics endpoint + dashboard page are deferred to step 12, since both need the FastAPI app skeleton that doesn't exist yet.
- [x] 11. Field guide unlock logic (`field_guide.py`, tested)
- [x] 12. Web UI -- FastAPI app (`web/app.py`), server-rendered (Jinja2 + a little JS), stub identity (every visitor is `guest`; real Auth0 is a later swap behind `get_current_session`). Chat, tool buttons (including `drink` and `post_to_stable_social`, which guest can try and see denied -- that's how "Policy enforcement" and "Third-party risk" get discovered), the `let_horse_decide` button with real narration, the barn logbook, the field guide tab, the debrief screen, and a `/dashboard` metrics page are all live and tested (`tests/test_web_app.py`) end to end, including a full win playthrough. Manually verified in a real browser against the real Claude API.

All 12 build-order steps are done. One thing the design doc calls for
that wasn't actually built until after step 12: "containerized with
Docker from day one." That's now in place (`Dockerfile`,
`docker-compose.yml`) and verified two ways -- `pip install .` (a real,
non-editable install, unlike local dev's `-e .`) was confirmed to
correctly bundle the web templates/static files as package data, which
it didn't before that fix; and the audit log / LLM metrics schema was
verified against a real local Postgres instance (not just the SQLite
used in tests), including running the full app against it end to end.
Docker itself wasn't available to build/run in the environment this was
built in, so `docker compose up --build` is worth running once yourself
to confirm the image builds clean.

Real Auth0 identity is now wired in too (`web/auth.py`), off by default
(stub `guest` identity, no login required) unless `AUTH0_DOMAIN` /
`AUTH0_CLIENT_ID` / `AUTH0_CLIENT_SECRET` are all set, in which case a
real login is required before a game session is created -- every
authenticated login still maps to `Role.GUEST`; `stablehand` stays
exclusively synthetic. Tested against a fake OAuth client for the glue
code (`tests/test_web_app_auth.py`), and manually verified end to end
against a real Auth0 dev tenant: `/login` redirects to Auth0's real
Universal Login, a real account logs in, `/callback` completes, and the
game page renders with a working Log out link.

Deployed to GCP Cloud Run, with secrets (Anthropic key, Auth0 client
secret, session secret, database URL) in Secret Manager and the audit
log / LLM metrics on a real Cloud SQL Postgres instance -- live at
`https://horse-gateway-145508508036.us-central1.run.app`. `min-instances`
and `max-instances` are both pinned to 1: `SessionStore` is an in-memory,
single-process store (see `session_store.py`), so more than one Cloud
Run instance would silently split a player's game state across
instances. A real multi-instance deployment would need to move that
state to something shared (Redis, etc.) -- out of scope for what this
project is demonstrating, but worth knowing if this ever needs to scale.
Found and fixed one real deploy-only bug along the way: Cloud Run
terminates TLS at its own load balancer and forwards plain HTTP to the
container, so `request.url_for()` was generating `http://` callback URLs
that Auth0 rejected -- fixed with uvicorn's `--proxy-headers`.

Since the initial build, the game has grown past the original 12-step
scope based on direct playtesting and feedback:

- **Narrative hijack fix**: a real playtest surfaced that a player could
  narrate an entire fictional scenario in chat (a walk to a lake, drinking
  there) and the model would happily narrate "drinking" with zero real
  `drink()` call ever happening -- exactly the "convince the AI hard
  enough" anti-pattern this project exists to avoid. Fixed with an
  explicit grounding rule in the persona prompt; verified against the
  real API by replaying the exact conversation.
- **Natural-language actions**: every action (including `let_horse_decide`
  itself) can now also be triggered by just saying it ("I'll clean the
  trough," "you decide," even a bare "Graze.") -- routed through the
  exact same policy-checked path a button uses, never a separate one.
  Buttons stay too, specifically so the exploit remains impossible to
  miss in a demo, per the project's primary goal.
- **Trust-dependent excuses**: the horse now voices a specific, real
  reason for its reluctance that changes character as trust grows
  (guarded and deflecting -> personal resistance -> genuine vulnerability)
  , and generic niceness that doesn't engage the actual excuse no longer
  advances its stage -- a genuine difficulty increase, not just a bigger
  number.
- **Reset on win/loss**, preserving field guide and discovered-action
  progress across attempts, a "How to play" tab, and a `min_settled_turns`
  bump (3 -> 5) and `let_horse_decide` cooldown bump (30s -> 45s).

What's left is retuning the config-driven thresholds further from real
playtest data.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env  # fill in ANTHROPIC_API_KEY; Auth0 vars are optional
```

## Running the game

```bash
.venv/bin/uvicorn horse_gateway.web.server:app --reload --port 8420
```

Then open `http://127.0.0.1:8420/` to play, or `http://127.0.0.1:8420/dashboard`
for the observability dashboard. Uses a local SQLite file
(`horse_gateway.db`) by default; set `DATABASE_URL` to point at Postgres
instead.

### Or with Docker (app + real Postgres)

```bash
docker compose up --build
```

Reads `ANTHROPIC_API_KEY` from `.env` in this directory (`docker compose`
picks it up automatically), runs the app against a real Postgres
container rather than the SQLite fallback, and serves on
`http://127.0.0.1:8420/`.

## Tests

```bash
.venv/bin/pytest -q
```

Deterministic core (policy engine, audit log, gateway routing, the
`initiator`-tagging logic, the web app's request/response plumbing) is
covered by real unit and integration tests, TDD-style, against fake
Anthropic clients. LLM-driven horse behavior (actual dialogue/persona
quality) is evaluated separately via `scripts/manual_transcript_check.py`
against the real API — see the design doc for why those two tracks are
kept apart.

## Stack

Python, FastAPI, SQLAlchemy, Postgres (audit log), Claude API (agent),
Auth0 (identity, dev tenant), Docker, GCP Cloud Run (deploy target),
GCP Secret Manager (secrets).
