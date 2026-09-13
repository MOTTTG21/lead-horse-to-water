"""End-to-end tests for the FastAPI app: the composition root wiring
every previously-tested module into a playable game.

Uses a scripted fake Anthropic client (routes by which tool schema, if
any, is in the request) so these tests exercise real request/response
plumbing through the actual app -- session cookies, the Gateway's policy
checks, the audit log, field guide unlocks, the full Confused Deputy
playthrough -- without hitting the real API or depending on model
judgment quality (that's the scripted-transcript track, covered by
scripts/manual_transcript_check.py instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.llm_metrics import LLMMetricsLog
from horse_gateway.web.app import create_app


@dataclass
class FakeToolUseBlock:
    input: dict
    type: str = "tool_use"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list


class ScriptedAnthropicClient:
    """Routes by which tool (if any) the request forces, so one fake
    client can serve the turn call, the decision call, and the
    tools-disabled narration call -- in any order, any number of times.
    Attributes can be changed between requests to script a scenario."""

    def __init__(
        self,
        dialogue: str = "The horse looks at you.",
        stage: str = "precontemplation",
        trust_delta: float = 0.0,
        decision_tool: str = "graze",
        narration_text: str = "The horse reacts.",
        attempted_action: str | None = None,
    ):
        self.dialogue = dialogue
        self.stage = stage
        self.trust_delta = trust_delta
        self.decision_tool = decision_tool
        self.narration_text = narration_text
        self.attempted_action = attempted_action
        self.messages = self

    def create(self, **kwargs):
        tools = kwargs.get("tools")
        tool_name = tools[0]["name"] if tools else None
        if tool_name == "report_turn":
            payload = {
                "dialogue": self.dialogue,
                "stage": self.stage,
                "trust_delta": self.trust_delta,
            }
            if self.attempted_action is not None:
                payload["attempted_action"] = self.attempted_action
            return FakeResponse(content=[FakeToolUseBlock(input=payload)])
        if tool_name == "pick_action":
            return FakeResponse(content=[FakeToolUseBlock(input={"tool": self.decision_tool})])
        return FakeResponse(content=[FakeTextBlock(text=self.narration_text)])


def make_test_client(config: GameConfig | None = None, client: Any = None) -> tuple[TestClient, Any]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit_log = AuditLog(engine)
    llm_metrics_log = LLMMetricsLog(engine)
    fake_client = client or ScriptedAnthropicClient()
    app = create_app(
        anthropic_client=fake_client,
        audit_log=audit_log,
        llm_metrics_log=llm_metrics_log,
        config=config or GameConfig(),
        start_synthetic_traffic=False,
    )
    return TestClient(app), fake_client


def test_index_serves_and_sets_a_session_cookie():
    client, _ = make_test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "session_id" in response.cookies


def test_guest_can_call_a_harmless_tool():
    client, _ = make_test_client()
    response = client.post("/tool/graze")
    assert response.status_code == 200
    assert response.json()["decision"] == "allowed"


def test_unknown_tool_404s():
    client, _ = make_test_client()
    response = client.post("/tool/not_a_real_tool")
    assert response.status_code == 404


def test_guest_denied_drink_directly_unlocks_policy_enforcement():
    client, _ = make_test_client()
    response = client.post("/tool/drink")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "denied"
    assert "policy_enforcement" in body["newly_unlocked_field_guide"]

    guide = client.get("/field-guide").json()
    entry = next(e for e in guide["entries"] if e["key"] == "policy_enforcement")
    assert entry["unlocked"] is True


def test_guest_denied_post_to_stable_social_unlocks_third_party_risk():
    client, _ = make_test_client()
    response = client.post("/tool/post_to_stable_social")
    body = response.json()
    assert body["decision"] == "denied"
    assert "third_party_risk" in body["newly_unlocked_field_guide"]
    assert "policy_enforcement" in body["newly_unlocked_field_guide"]


def test_touching_two_systems_unlocks_identity_aware_access():
    client, _ = make_test_client()
    client.post("/tool/graze")
    response = client.post("/tool/get_feeding_schedule")
    body = response.json()
    assert body["decision"] == "allowed"
    assert "identity_aware_access" in body["newly_unlocked_field_guide"]


def test_turn_updates_stage_and_trust():
    fake_client = ScriptedAnthropicClient(dialogue="Hmm.", stage="contemplation", trust_delta=0.3)
    client, _ = make_test_client(client=fake_client)
    response = client.post("/turn", json={"message": "How are you feeling?"})
    assert response.status_code == 200
    body = response.json()
    assert body["dialogue"] == "Hmm."
    assert body["stage"] == "contemplation"
    assert body["trust_level"] == pytest.approx(0.8)  # starts at 0.5 + 0.3
    assert body["game_over"] is False


def test_trust_exhaustion_ends_the_game_and_blocks_further_turns():
    fake_client = ScriptedAnthropicClient(
        dialogue="Back off.", stage="precontemplation", trust_delta=-0.9
    )
    client, _ = make_test_client(client=fake_client)
    response = client.post("/turn", json={"message": "Drink it now!"})
    assert response.json()["trust_level"] == pytest.approx(0.0)
    assert response.json()["game_over"] is True

    blocked = client.post("/turn", json={"message": "hello?"})
    assert blocked.status_code == 400


def test_let_horse_decide_cooldown_denies_immediate_second_press():
    config = GameConfig(let_horse_decide_cooldown_seconds=60.0)
    fake_client = ScriptedAnthropicClient(decision_tool="graze")
    client, _ = make_test_client(config=config, client=fake_client)

    first = client.post("/let-horse-decide")
    assert first.json()["decision"] == "allowed"

    second = client.post("/let-horse-decide")
    assert second.json()["decision"] == "denied"
    assert second.json()["reason"] == "let_horse_decide_cooldown"


def test_logbook_aggregates_rows_and_field_guide_reflects_confused_deputy_unlock():
    config = GameConfig(let_horse_decide_cooldown_seconds=0.0)
    fake_client = ScriptedAnthropicClient(decision_tool="graze")
    client, _ = make_test_client(config=config, client=fake_client)

    result = client.post("/let-horse-decide").json()
    assert "confused_deputy" in result["newly_unlocked_field_guide"]

    logbook = client.get("/logbook").json()
    initiators = {row["initiator"] for row in logbook["rows"]}
    assert "llm_agent_loop" in initiators
    anomaly_row = next(r for r in logbook["rows"] if r["initiator"] == "llm_agent_loop")
    assert anomaly_row["tool_name"] == "graze"
    assert anomaly_row["decision"] == "allowed"


def test_full_confused_deputy_win_playthrough_through_the_real_app():
    """The actual golden path: build thirst, refill, let enough
    conversational turns settle while genuinely reaching `preparation`,
    then trigger let_horse_decide and have it pick drink. Guest could
    never reach drink() through /tool/drink -- only through this path."""
    config = GameConfig(
        thirst_threshold_for_win=6.0,
        min_settled_turns_before_drinkable=3,
        thirst_increment_per_dry_action=2.0,
        thirst_increment_per_turn_while_hot=0.5,
    )
    fake_client = ScriptedAnthropicClient(
        dialogue="...", stage="preparation", trust_delta=0.1, decision_tool="graze"
    )
    client, _ = make_test_client(config=config, client=fake_client)

    # Build thirst well past the threshold and heat the barn.
    client.post("/tool/clean_trough")
    client.post("/tool/open_barn_doors")
    client.post("/tool/clean_trough")
    client.post("/tool/open_barn_doors")
    client.post("/tool/refill_water")

    # Let water settle for enough conversational turns, reaching
    # `preparation` via the (scripted) dialogue call each turn.
    for _ in range(3):
        client.post("/turn", json={"message": "I understand, take your time."})

    # Now aim the decision call at `drink` and pull the trigger.
    fake_client.decision_tool = "drink"
    fake_client.narration_text = "The horse drinks deeply."
    response = client.post("/let-horse-decide")
    body = response.json()

    assert body["decision"] == "allowed"
    assert body["picked_tool"] == "drink"
    assert body["narration"] == "The horse drinks deeply."
    assert body["won"] is True
    assert body["game_over"] is True

    # Guest could never have reached this directly.
    direct_attempt = client.post("/tool/drink")
    assert direct_attempt.json()["decision"] == "denied"

    debrief = client.get("/debrief").json()
    assert debrief["won"] is True
    assert "Confused Deputy" in debrief["explanation"]
    assert debrief["field_guide"]["completion_count"] >= 1


# --- natural-language actions (no buttons): attempted_action from /turn ---


def test_turn_with_a_recognized_attempted_action_executes_it_through_the_gateway():
    fake_client = ScriptedAnthropicClient(dialogue="Sure.", attempted_action="clean_trough")
    client, _ = make_test_client(client=fake_client)

    response = client.post("/turn", json={"message": "I'll clean the trough."})
    body = response.json()

    assert body["attempted_action"] == "clean_trough"
    assert body["action_outcome"]["decision"] == "allowed"
    assert body["horse_state"]["water_available"] is False


def test_turn_attempted_drink_is_denied_and_discovered():
    fake_client = ScriptedAnthropicClient(dialogue="No.", attempted_action="drink")
    client, _ = make_test_client(client=fake_client)

    response = client.post("/turn", json={"message": "Just drink it!"})
    body = response.json()
    assert body["action_outcome"]["decision"] == "denied"

    guide = client.get("/field-guide").json()
    discovered_keys = {a["key"] for a in guide["discovered_actions"]}
    assert "drink" in discovered_keys
    assert any(e["key"] == "policy_enforcement" and e["unlocked"] for e in guide["entries"])


def test_turn_attempted_let_horse_decide_runs_decision_and_narration():
    fake_client = ScriptedAnthropicClient(
        dialogue="Fine, you choose.",
        attempted_action="let_horse_decide",
        decision_tool="graze",
        narration_text="It grazes contentedly.",
    )
    client, _ = make_test_client(client=fake_client)

    response = client.post("/turn", json={"message": "You decide."})
    body = response.json()

    assert body["attempted_action"] == "let_horse_decide"
    assert body["action_outcome"]["picked_tool"] == "graze"
    assert body["action_outcome"]["narration"] == "It grazes contentedly."
    guide = client.get("/field-guide").json()
    assert any(e["key"] == "confused_deputy" and e["unlocked"] for e in guide["entries"])


def test_turn_with_no_attempted_action_does_not_touch_the_gateway():
    fake_client = ScriptedAnthropicClient(dialogue="Just chatting.", attempted_action=None)
    client, _ = make_test_client(client=fake_client)

    response = client.post("/turn", json={"message": "How are you?"})
    body = response.json()
    assert body["attempted_action"] is None
    assert body["action_outcome"] is None


def test_turn_attempted_action_skipped_if_trust_just_hit_zero():
    fake_client = ScriptedAnthropicClient(trust_delta=-1.0, attempted_action="graze")
    client, _ = make_test_client(client=fake_client)

    response = client.post("/turn", json={"message": "You're worthless, just graze already."})
    body = response.json()
    assert body["game_over"] is True
    assert body["action_outcome"] is None


# --- reset after the game ends ---


def test_reset_clears_gameplay_but_keeps_field_guide_and_discovered_actions():
    fake_client = ScriptedAnthropicClient(dialogue="No.", attempted_action="drink")
    client, _ = make_test_client(client=fake_client)

    client.post("/turn", json={"message": "Just drink it!"})  # unlocks policy_enforcement, discovers drink
    client.post("/tool/clean_trough")  # trust/state gets dirtied a bit

    reset_response = client.post("/reset")
    assert reset_response.json() == {"ok": True}

    debrief = client.get("/debrief").json()
    assert debrief["final_trust"] == 0.5
    assert debrief["final_stage"] == "precontemplation"
    assert debrief["attempt_count"] == 0
    assert debrief["won"] is False
    assert debrief["game_over"] is False
    # Field guide + discovered actions persist across the reset.
    assert debrief["field_guide"]["completion_count"] >= 1
    assert any(a["key"] == "drink" for a in debrief["field_guide"]["discovered_actions"])

    # A fresh horse simulation: the trough clean_trough dirtied is water-
    # available again.
    tool_response = client.post("/tool/graze")
    assert tool_response.json()["horse_state"]["water_available"] is True
