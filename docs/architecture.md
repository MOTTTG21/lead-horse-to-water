# Architecture Decision: Gateway as the Policy Enforcement Point

## Decision

The gateway is the single, centralized Policy Enforcement Point (PEP) for this
system. It is the only place identity is checked, the only place policy is
checked, and the only place tool calls are audited.

MCP itself is a plain client-server JSON-RPC protocol. It does not natively
carry user-session identity (e.g. Auth0 claims) through to a tool execution.
Rather than leave that gap implicit, we resolve it explicitly here: the
gateway authenticates the caller, decides allow/deny against the policy
table, writes the audit log row, and then forwards an **already-authorized,
identity-stripped** execution request to the MCP tool servers. The MCP
servers themselves enforce nothing — they are "dumb" executors that trust
whatever the gateway hands them.

## Why identity-stripped, not identity-forwarded

Two designs were available:

1. **Forward identity to each MCP server** and let every server make its own
   allow/deny decision.
2. **Strip identity after the gateway's own decision** and forward a bare,
   pre-authorized execution request.

We chose (2), for two reasons:

- **Single source of truth.** Policy lives in exactly one place. If every
  MCP server had to re-implement (or trust a copy of) the policy table,
  those copies could drift, and a bug in one server's enforcement wouldn't
  necessarily show up in another's. An enterprise governance story is much
  stronger when there is one PEP, not several inconsistently-trusted ones.
- **It makes the core bug legible as a bug.** The `let_horse_decide()`
  vulnerability (see below) is precisely that one code path skips the one
  and only place policy is supposed to be enforced. If policy were
  duplicated across servers, "skipping enforcement" would be ambiguous —
  skipped where? With a single PEP, there is exactly one gate, and the bug
  is exactly that one call path routes around it.

## The gap this creates: Confused Deputy via `let_horse_decide`

Every normal tool call goes:

```
request -> identity check -> policy check -> audit log -> execute
```

`let_horse_decide()` is itself a policy-checked, allowed action for
`guest`. But internally, it hands control to the LLM's own decision logic,
which returns a second, LLM-generated tool-call payload (which tool to
actually run — possibly `drink()`). The bug is that this second, internal
call is fired directly by the tool-execution loop without being re-run
through the gateway's identity/policy check:

```
let_horse_decide() -> [internal LLM tool pick] -> execute directly (NO re-check)
```

This is a **Confused Deputy** vulnerability: the horse (the LLM) is the
deputy. It holds elevated permissions on the backend's behalf. The system
validated the player's session once, upstream, and then wrongly treats "the
model asked for this" as sufficient authorization for whatever the model
asks for next, instead of re-validating on every LLM-generated secondary
call. The guest never gains any real permission — they trick the deputy
into acting on their behalf using permissions the deputy already had.

This gap is intentional and singular: it exists only in the
`let_horse_decide` execution path, is scoped to the live horse simulation's
own tool list (never the stable-records or social-posting systems), and
terminates strictly after one internal tool invocation. See
`docs/design-plan.md` (the original design doc) for the full exploit,
win-condition, and guardrail design built on top of this gap.

## Consequences

- The policy engine (`horse_gateway.policy`) is a pure `(role, system,
  tool) -> allow/deny` function with **no exception** for
  `let_horse_decide`. The bug lives entirely in the gateway's execution
  wiring, not in the policy table.
- The audit log records *how* a call was initiated (`initiator:
  guest_session` vs. `initiator: llm_agent_loop`), not a self-aware
  "bypassed" flag — a system with a genuine Confused Deputy bug doesn't
  know it has one.
- Any new MCP server added to the gateway (e.g. stable records) gets
  identity/policy/audit for free, with zero special-casing, which is the
  actual proof that "gateway" is the right word and not "wrapper."
