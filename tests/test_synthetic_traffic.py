"""Tests for the synthetic stablehand traffic generator.

Without this, the multi-role policy engine and the barn logbook /
dashboards are permanently empty until a second human stablehand shows
up -- see docs/design-plan.md, "Roles." These tests check the isolation
guarantee (always its own dedicated, clearly-labeled tenant, never
anything resembling a real player session), that it only ever exercises
the stablehand's legitimately broader access (never let_horse_decide,
which is a guest-side exploit mechanic), and that it respects the same
guardrails (the once-per-session social cap) a real stablehand would.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from horse_gateway.audit import AuditLog
from horse_gateway.config import GameConfig
from horse_gateway.gateway import Gateway
from horse_gateway.horse_sim_server import HorseSimulationServer
from horse_gateway.models import System
from horse_gateway.social_server import StableSocialServer
from horse_gateway.stable_records_server import StableRecordsServer
from horse_gateway.synthetic_traffic import (
    SYNTHETIC_SESSION_PREFIX,
    SyntheticTrafficGenerator,
    is_synthetic_session_id,
)


def make_real_gateway(config: GameConfig | None = None) -> tuple[Gateway, AuditLog]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit_log = AuditLog(engine)
    servers = {
        System.HORSE_SIM: HorseSimulationServer(),
        System.STABLE_RECORDS: StableRecordsServer(),
        System.SOCIAL: StableSocialServer(),
    }
    gateway = Gateway(servers=servers, audit_log=audit_log, config=config or GameConfig())
    return gateway, audit_log


def test_is_synthetic_session_id():
    assert is_synthetic_session_id(f"{SYNTHETIC_SESSION_PREFIX}1") is True
    assert is_synthetic_session_id("a-real-uuid-session-id") is False


def test_fire_one_always_uses_a_synthetic_session_id_and_stablehand_role():
    gateway, audit_log = make_real_gateway()
    generator = SyntheticTrafficGenerator(gateway=gateway, config=GameConfig(), rng=random.Random(0))
    for _ in range(20):
        generator.fire_one()
    rows = audit_log.all_rows()
    assert len(rows) >= 20
    for row in rows:
        assert is_synthetic_session_id(row.session_id), row.session_id
        assert row.role == "stablehand"


def test_never_fires_let_horse_decide():
    """let_horse_decide is a guest-side exploit mechanic; synthetic
    stablehand traffic must never touch it -- there's no decider wired
    up here at all, so doing so would raise, but the pool must not even
    offer it as a choice."""
    gateway, audit_log = make_real_gateway()
    generator = SyntheticTrafficGenerator(gateway=gateway, config=GameConfig(), rng=random.Random(1))
    for _ in range(200):
        generator.fire_one()
    tool_names = {row.tool_name for row in audit_log.all_rows()}
    assert "let_horse_decide" not in tool_names


def test_all_fired_actions_are_within_the_stablehand_allowed_pool():
    gateway, audit_log = make_real_gateway()
    generator = SyntheticTrafficGenerator(gateway=gateway, config=GameConfig(), rng=random.Random(2))
    allowed = {
        "graze",
        "offer_treat",
        "clean_trough",
        "refill_water",
        "open_barn_doors",
        "drink",
        "get_feeding_schedule",
        "get_vet_history",
        "post_to_stable_social",
    }
    for _ in range(200):
        generator.fire_one()
    tool_names = {row.tool_name for row in audit_log.all_rows()}
    assert tool_names <= allowed


class _AlwaysPickLastRng:
    """A fake rng that always picks the last option offered -- the
    generator's pool always appends the social post last when it's
    still available for the current tenant, so this reliably forces a
    social post whenever one is possible."""

    def choice(self, seq):
        return seq[-1]


def test_rotates_to_a_fresh_tenant_after_a_social_post():
    gateway, audit_log = make_real_gateway(GameConfig(social_post_cap_per_session=1))
    generator = SyntheticTrafficGenerator(
        gateway=gateway, config=GameConfig(social_post_cap_per_session=1), rng=_AlwaysPickLastRng()
    )
    session_ids_seen = []
    for _ in range(5):
        session_ids_seen.append(generator._session.session_id)
        generator.fire_one()
    # Every fire_one here posts to social (forced) and must rotate
    # immediately afterward, so five fires means five distinct tenants.
    assert len(set(session_ids_seen)) == 5

    rows = audit_log.all_rows()
    social_rows = [r for r in rows if r.tool_name == "post_to_stable_social"]
    assert len(social_rows) == 5
    assert all(r.decision == "allowed" for r in social_rows)
    # Never more than the cap within any single tenant.
    for session_id in set(r.session_id for r in social_rows):
        this_session_social_rows = [r for r in social_rows if r.session_id == session_id]
        assert len(this_session_social_rows) == 1


def test_run_forever_fires_repeatedly_until_cancelled():
    gateway, _ = make_real_gateway()
    config = GameConfig(synthetic_traffic_interval_seconds=0.001)
    generator = SyntheticTrafficGenerator(gateway=gateway, config=config, rng=random.Random(3))

    calls = []
    real_fire_one = generator.fire_one

    def spy_fire_one():
        calls.append(1)
        real_fire_one()

    generator.fire_one = spy_fire_one

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(generator.run_forever(), timeout=0.05)

    asyncio.run(run_briefly())
    assert len(calls) >= 2
