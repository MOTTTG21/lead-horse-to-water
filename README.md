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

All 12 build-order steps are done. What's left is beyond the original
build order: wiring real Auth0 identity in behind `get_current_session`,
deploying to GCP Cloud Run with secrets in Secret Manager, and retuning
the config-driven thresholds from real playtest data.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env  # fill in ANTHROPIC_API_KEY
```

## Running the game

```bash
.venv/bin/uvicorn horse_gateway.web.server:app --reload --port 8420
```

Then open `http://127.0.0.1:8420/` to play, or `http://127.0.0.1:8420/dashboard`
for the observability dashboard. Uses a local SQLite file
(`horse_gateway.db`) by default; set `DATABASE_URL` to point at Postgres
instead.

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
