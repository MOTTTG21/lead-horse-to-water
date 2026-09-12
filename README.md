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
- [ ] 11. Field guide unlock logic
- [ ] 12. Web UI (chat, barn logbook, field guide tab, debrief screen, metrics dashboard)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Tests

```bash
.venv/bin/pytest -q
```

Deterministic core (policy engine, audit log, gateway routing, the
`initiator`-tagging logic) is covered by real unit tests, TDD-style. LLM-
driven horse behavior will be covered separately by scripted-transcript
evaluation, not unit tests — see the design doc for why those two tracks
are kept apart.

## Stack

Python, FastAPI, SQLAlchemy, Postgres (audit log), Claude API (agent),
Auth0 (identity, dev tenant), Docker, GCP Cloud Run (deploy target),
GCP Secret Manager (secrets).
