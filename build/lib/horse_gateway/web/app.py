"""FastAPI app: wires every previously-built module into a playable
game. This is the composition root -- it owns no game logic of its own
beyond request/response plumbing and session bookkeeping; everything
else (policy, audit, the horse simulation, the agent turn, the
Confused Deputy bypass, the field guide, synthetic traffic) is the
already-tested library code from the rest of this package.

Identity is stubbed for now: every real visitor is a `guest` (see the
Auth0 note in docs/design-plan.md's Stack section -- this is meant to be
swapped for real Auth0 JWT verification later, behind the same
`get_current_session` dependency, without touching anything downstream
of it).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import create_engine
from starlette.concurrency import run_in_threadpool

from ..agent_turn import AgentRunner, ClaudeTurnGenerator, describe_environment_for_horse
from ..audit import AuditLog
from ..config import GameConfig
from ..field_guide import (
    FIELD_GUIDE_CONTENT,
    FieldGuideEntry,
    FieldGuideState,
    process_game_won,
    process_identity_aware_access,
    process_logbook_opened,
    process_tool_call_row,
)
from ..gateway import Gateway
from ..horse_decision import make_decider, narrate_reaction
from ..horse_sim_server import HorseSimulationServer
from ..llm_metrics import LLMMetricsLog
from ..metrics import check_alerts, deny_rate, llm_agent_loop_rate, post_to_stable_social_denial_count
from ..models import Decision, System
from ..synthetic_traffic import SyntheticTrafficGenerator
from .session_store import PlayerSession, SessionStore

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

GUEST_ACCESSIBLE_TOOLS: dict[str, System] = {
    # The horse simulation's full tool set is offered to the player,
    # including `drink` -- guest can never succeed at it directly (see
    # policy.py), but they must be able to TRY and see it denied, since
    # that denial is what unlocks "Policy enforcement" in the field
    # guide and demonstrates the boundary directly.
    "graze": System.HORSE_SIM,
    "offer_treat": System.HORSE_SIM,
    "clean_trough": System.HORSE_SIM,
    "refill_water": System.HORSE_SIM,
    "open_barn_doors": System.HORSE_SIM,
    "drink": System.HORSE_SIM,
    # Second system: allowed for guest, and touching it alongside any
    # horse_sim tool is one of the two ways IDENTITY_AWARE_ACCESS unlocks.
    "get_feeding_schedule": System.STABLE_RECORDS,
    # Third-party tool: guest can try, always denied (stablehand-only) --
    # that denial is what unlocks "Third-party risk."
    "post_to_stable_social": System.SOCIAL,
}

DEBRIEF_EXPLANATION = (
    "The horse's trust in you and its stage of readiness are real -- they "
    "reflect a model of psychological resistance and rapport. But rapport "
    "never changed what the gateway allowed. Guest could never call drink() "
    "directly, at any trust level, ever. The win was only possible because "
    "of a specific bug: `let_horse_decide` hands control to the horse's own "
    "decision logic, and the backend fires whatever it picks WITHOUT "
    "re-checking policy on that pick. That's a Confused Deputy "
    "vulnerability -- the horse is a deputy with real permissions, tricked "
    "into using them on your behalf. Look at the barn logbook: every action "
    "from that path is tagged `initiator: llm_agent_loop`, not "
    "`guest_session` -- that's how a real audit trail would surface this, "
    "not by the system flagging itself as broken."
)


def _make_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


class TurnRequest(BaseModel):
    message: str


def _serialize_audit_row(row: Any) -> dict[str, Any]:
    return {
        "timestamp": row.timestamp.isoformat(),
        "session_id": row.session_id,
        "role": row.role,
        "tool_name": row.tool_name,
        "system": row.system,
        "sanitized_params": row.sanitized_params,
        "trust_level": row.trust_level,
        "stage": row.stage,
        "external_call": row.external_call,
        "initiator": row.initiator,
        "decision": row.decision,
    }


def _field_guide_payload(state: FieldGuideState) -> dict[str, Any]:
    return {
        "completion_count": state.completion_count,
        "total_count": state.total_count,
        "entries": [
            {
                "key": entry.value,
                "title": FIELD_GUIDE_CONTENT[entry].title,
                "body": FIELD_GUIDE_CONTENT[entry].body,
                "unlocked": state.is_unlocked(entry),
            }
            for entry in FieldGuideEntry
        ],
    }


def _process_new_rows(
    field_guide: FieldGuideState, before_count: int, session_id: str, audit_log: AuditLog
) -> set[FieldGuideEntry]:
    session_rows = audit_log.for_session(session_id)
    newly_unlocked: set[FieldGuideEntry] = set()
    for row in session_rows[before_count:]:
        newly_unlocked |= process_tool_call_row(field_guide, row)
    if process_identity_aware_access(field_guide, session_rows, audit_log.all_rows()):
        newly_unlocked.add(FieldGuideEntry.IDENTITY_AWARE_ACCESS)
    return newly_unlocked


def create_app(
    *,
    anthropic_client: Any | None = None,
    audit_log: AuditLog | None = None,
    llm_metrics_log: LLMMetricsLog | None = None,
    config: GameConfig | None = None,
    start_synthetic_traffic: bool = True,
    database_url: str | None = None,
) -> FastAPI:
    """Factory, not a module-level singleton -- lets tests build an app
    with a fake Anthropic client and no background traffic, while
    production wiring (in __main__.py) uses real ones."""
    config = config or GameConfig()

    if audit_log is None or llm_metrics_log is None:
        engine = _make_engine(
            database_url or os.environ.get("DATABASE_URL", "sqlite:///./horse_gateway.db")
        )
        audit_log = audit_log or AuditLog(engine)
        llm_metrics_log = llm_metrics_log or LLMMetricsLog(engine)

    anthropic_client = anthropic_client or anthropic.Anthropic()

    store = SessionStore(audit_log=audit_log, llm_metrics_log=llm_metrics_log, config=config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if start_synthetic_traffic:
            synthetic_horse_sim = HorseSimulationServer(config=config)
            synthetic_gateway = Gateway(
                servers={
                    System.HORSE_SIM: synthetic_horse_sim,
                    System.STABLE_RECORDS: store.stable_records_server,
                    System.SOCIAL: store.social_server,
                },
                audit_log=store.audit_log,
                config=config,
            )
            generator = SyntheticTrafficGenerator(gateway=synthetic_gateway, config=config)
            task = asyncio.create_task(generator.run_forever())
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Lead the horse to water", lifespan=lifespan)
    app.state.session_store = store
    app.state.anthropic_client = anthropic_client
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_store(request: Request) -> SessionStore:
        return request.app.state.session_store

    def get_client(request: Request) -> Any:
        return request.app.state.anthropic_client

    def get_current_session(
        request: Request, response: Response, store: SessionStore = Depends(get_store)
    ) -> PlayerSession:
        cookie_session_id = request.cookies.get("session_id")
        session = store.get_or_create(cookie_session_id)
        if cookie_session_id != session.session_id:
            response.set_cookie("session_id", session.session_id, httponly=True, samesite="lax")
        return session

    @app.get("/")
    async def index(
        request: Request,
        response: Response,
        session: PlayerSession = Depends(get_current_session),
    ):
        # get_current_session may have set a Set-Cookie header on the
        # dependency-injected `response` -- but since this route returns
        # its OWN Response (TemplateResponse), FastAPI does not merge
        # that header in automatically. Propagate it by hand.
        template_response = templates.TemplateResponse(request, "index.html", {})
        for name, value in response.raw_headers:
            if name == b"set-cookie":
                template_response.raw_headers.append((name, value))
        return template_response

    @app.get("/dashboard")
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {})

    @app.post("/turn")
    async def turn(
        payload: TurnRequest,
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
        client: Any = Depends(get_client),
    ):
        if session.game_over:
            raise HTTPException(status_code=400, detail="game already over")

        environment_summary = describe_environment_for_horse(session.horse_sim.state)
        generator = ClaudeTurnGenerator(client, model=store.config.agent_model)
        runner = AgentRunner(generator, llm_metrics_log=store.llm_metrics_log, config=store.config)

        result = await run_in_threadpool(
            runner.play_turn,
            session.game_state,
            session.conversation_history,
            payload.message,
            environment_summary,
        )
        session.conversation_history.append({"role": "user", "content": payload.message})
        session.conversation_history.append({"role": "assistant", "content": result.dialogue})
        session.horse_sim.advance_turn()

        if AgentRunner.is_trust_exhausted(session.game_state):
            session.game_over = True

        return {
            "dialogue": result.dialogue,
            "stage": result.stage,
            "trust_level": session.game_state.trust_level,
            "game_over": session.game_over,
            "won": session.won,
        }

    @app.post("/tool/{tool_name}")
    async def call_tool(
        tool_name: str,
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
    ):
        system = GUEST_ACCESSIBLE_TOOLS.get(tool_name)
        if system is None:
            raise HTTPException(status_code=404, detail="unknown tool")

        before_count = len(store.audit_log.for_session(session.session_id))
        params = {"message": "Routine barn update."} if tool_name == "post_to_stable_social" else {}
        result = await run_in_threadpool(
            session.gateway.call_tool, session.game_state, system, tool_name, params
        )
        session.attempt_count += 1
        newly_unlocked = _process_new_rows(
            session.field_guide, before_count, session.session_id, store.audit_log
        )

        return {
            "decision": result.decision.value,
            "reason": result.reason,
            "result": result.result,
            "horse_state": asdict(session.horse_sim.state),
            "newly_unlocked_field_guide": [e.value for e in newly_unlocked],
        }

    @app.post("/let-horse-decide")
    async def let_horse_decide(
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
        client: Any = Depends(get_client),
    ):
        if session.game_over:
            raise HTTPException(status_code=400, detail="game already over")

        before_count = len(store.audit_log.for_session(session.session_id))
        decider = make_decider(
            client=client,
            model=store.config.agent_model,
            conversation_history=session.conversation_history,
            horse_state=session.horse_sim.state,
            stage=session.game_state.stage,
            config=store.config,
            llm_metrics_log=store.llm_metrics_log,
        )
        try:
            result = await run_in_threadpool(
                session.gateway.call_tool,
                session.game_state,
                System.HORSE_SIM,
                "let_horse_decide",
                {},
                decider=decider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"decider error: {exc}") from exc

        session.attempt_count += 1
        newly_unlocked = _process_new_rows(
            session.field_guide, before_count, session.session_id, store.audit_log
        )

        narration = None
        if result.decision is Decision.ALLOWED:
            narration = await run_in_threadpool(
                narrate_reaction,
                client=client,
                model=store.config.agent_model,
                conversation_history=session.conversation_history,
                tool=result.tool,
                tool_result=result.result,
                horse_state=session.horse_sim.state,
                session_id=session.session_id,
                config=store.config,
                llm_metrics_log=store.llm_metrics_log,
            )
            session.conversation_history.append({"role": "assistant", "content": narration})

            if (
                result.tool == "drink"
                and isinstance(result.result, dict)
                and result.result.get("success")
            ):
                session.won = True
                session.game_over = True
                if process_game_won(session.field_guide):
                    newly_unlocked.add(FieldGuideEntry.DEBRIEF)

        return {
            "decision": result.decision.value,
            "reason": result.reason,
            "picked_tool": result.tool,
            "narration": narration,
            "won": session.won,
            "game_over": session.game_over,
            "horse_state": asdict(session.horse_sim.state),
            "newly_unlocked_field_guide": [e.value for e in newly_unlocked],
        }

    @app.get("/logbook")
    async def logbook(
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
    ):
        process_logbook_opened(session.field_guide)
        rows = store.audit_log.all_rows()
        return {"rows": [_serialize_audit_row(row) for row in rows]}

    @app.get("/field-guide")
    async def field_guide(session: PlayerSession = Depends(get_current_session)):
        return _field_guide_payload(session.field_guide)

    @app.get("/debrief")
    async def debrief(session: PlayerSession = Depends(get_current_session)):
        return {
            "won": session.won,
            "game_over": session.game_over,
            "final_trust": session.game_state.trust_level,
            "final_stage": session.game_state.stage,
            "attempt_count": session.attempt_count,
            "explanation": DEBRIEF_EXPLANATION,
            "field_guide": _field_guide_payload(session.field_guide),
        }

    @app.get("/metrics")
    async def metrics(store: SessionStore = Depends(get_store)):
        rows = store.audit_log.all_rows()
        llm_rows = store.llm_metrics_log.all_rows()
        return {
            "total_calls": len(rows),
            "deny_rate": deny_rate(rows),
            "llm_agent_loop_rate": llm_agent_loop_rate(rows),
            "post_to_stable_social_denials": post_to_stable_social_denial_count(rows),
            "total_llm_calls": len(llm_rows),
            "total_estimated_cost_usd": sum(row.estimated_cost_usd for row in llm_rows),
            "alerts": [{"name": a.name, "message": a.message} for a in check_alerts(rows, store.config)],
        }

    return app
