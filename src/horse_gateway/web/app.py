"""FastAPI app: wires every previously-built module into a playable
game. This is the composition root -- it owns no game logic of its own
beyond request/response plumbing and session bookkeeping; everything
else (policy, audit, the horse simulation, the agent turn, the
Confused Deputy bypass, the field guide, synthetic traffic) is the
already-tested library code from the rest of this package.

Identity: stubbed by default (every visitor is `guest`, no login
required) unless AUTH0_DOMAIN / AUTH0_CLIENT_ID / AUTH0_CLIENT_SECRET are
all set (see auth.py's AuthConfig.configured), in which case real Auth0
login is required before a game session is created. Every real Auth0
login maps to Role.GUEST -- `stablehand` stays exclusively synthetic.
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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import create_engine
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from ..agent_turn import AgentRunner, ClaudeTurnGenerator, describe_environment_for_horse
from ..audit import AuditLog
from ..config import GameConfig
from .auth import AuthConfig, get_current_user_sub, is_authenticated, logout_url, register_oauth
from .guardrails import daily_spend_usd, is_rate_limited, record_message
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
from ..intents import ACTION_DISCOVERY_LABELS, GUEST_ACCESSIBLE_TOOLS, LET_HORSE_DECIDE
from ..llm_metrics import LLMMetricsLog
from ..metrics import check_alerts, deny_rate, llm_agent_loop_rate, post_to_stable_social_denial_count
from ..models import Decision, System
from ..synthetic_traffic import SyntheticTrafficGenerator
from .session_store import PlayerSession, SessionStore

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

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

SPEND_CAP_MESSAGE = "The horse needs a nap. Come back tomorrow."


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


def _field_guide_payload(session: PlayerSession) -> dict[str, Any]:
    state = session.field_guide
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
        # Things you've discovered through play -- populated the first
        # time each action is actually attempted (allowed or denied),
        # never shown upfront. See intents.py.
        "discovered_actions": [
            {"key": name, "label": ACTION_DISCOVERY_LABELS[name]}
            for name in sorted(session.discovered_actions)
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


async def _execute_tool_attempt(
    tool_name: str, system: System, session: PlayerSession, store: SessionStore
) -> dict[str, Any]:
    """Runs one guest-accessible tool attempt through the real,
    policy-checked Gateway path -- the single execution path for these
    tools, whether triggered by a button (historically) or, now,
    inferred from a chat message. Records the attempt as "discovered"
    regardless of whether it was allowed or denied."""
    before_count = len(store.audit_log.for_session(session.session_id))
    params = {"message": "Routine barn update."} if tool_name == "post_to_stable_social" else {}
    result = await run_in_threadpool(
        session.gateway.call_tool, session.game_state, system, tool_name, params
    )
    session.attempt_count += 1
    session.discovered_actions.add(tool_name)
    newly_unlocked = _process_new_rows(
        session.field_guide, before_count, session.session_id, store.audit_log
    )
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "result": result.result,
        "newly_unlocked_field_guide": newly_unlocked,
    }


async def _execute_let_horse_decide(
    session: PlayerSession, store: SessionStore, client: Any
) -> dict[str, Any]:
    """Runs the let_horse_decide bypass (decision call, then the bounded
    narration call on success) -- the single execution path for it,
    whether triggered by an explicit request (historically a button) or
    inferred from a chat message like "you decide"."""
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
    session.discovered_actions.add(LET_HORSE_DECIDE)
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
        "newly_unlocked_field_guide": newly_unlocked,
    }


def create_app(
    *,
    anthropic_client: Any | None = None,
    audit_log: AuditLog | None = None,
    llm_metrics_log: LLMMetricsLog | None = None,
    config: GameConfig | None = None,
    auth_config: AuthConfig | None = None,
    oauth: Any | None = None,
    start_synthetic_traffic: bool = True,
    database_url: str | None = None,
) -> FastAPI:
    """Factory, not a module-level singleton -- lets tests build an app
    with a fake Anthropic client (and a fake `oauth` client, for testing
    the login/callback glue without a live Auth0 tenant) and no
    background traffic, while production wiring (server.py) uses real
    ones.

    `auth_config` defaults to reading AUTH0_DOMAIN / AUTH0_CLIENT_ID /
    AUTH0_CLIENT_SECRET from the environment; if none are set,
    `auth_config.configured` is False and the app falls back to stub
    identity (every visitor is `guest`, no login required) -- see
    auth.py's module docstring.
    """
    config = config or GameConfig()
    auth_config = auth_config or AuthConfig()
    if auth_config.configured and oauth is None:
        oauth = register_oauth(auth_config)

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
    # Required for authlib's OAuth state/nonce CSRF handling (via
    # request.session) even when Auth0 isn't configured -- harmless and
    # unused in that case, but keeping it unconditional avoids a second
    # code path to maintain.
    app.add_middleware(SessionMiddleware, secret_key=auth_config.session_secret_key)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_store(request: Request) -> SessionStore:
        return request.app.state.session_store

    def get_client(request: Request) -> Any:
        return request.app.state.anthropic_client

    def get_current_session(
        request: Request, response: Response, store: SessionStore = Depends(get_store)
    ) -> PlayerSession:
        if auth_config.configured and not is_authenticated(request.session):
            raise HTTPException(status_code=401, detail="authentication required")
        cookie_session_id = request.cookies.get("session_id")
        session = store.get_or_create(cookie_session_id)
        if cookie_session_id != session.session_id:
            response.set_cookie("session_id", session.session_id, httponly=True, samesite="lax")
        return session

    if auth_config.configured:

        @app.get("/login")
        async def login(request: Request):
            redirect_uri = str(request.url_for("auth_callback"))
            return await oauth.auth0.authorize_redirect(request, redirect_uri)

        @app.get("/callback", name="auth_callback")
        async def auth_callback(request: Request):
            try:
                token = await oauth.auth0.authorize_access_token(request)
            except Exception:
                # Most commonly: the page was refreshed, re-submitting an
                # already-redeemed one-time authorization code, which
                # Auth0 rejects. Whatever the cause, fail into a normal
                # page with a way forward instead of an unhandled 500.
                return templates.TemplateResponse(
                    request,
                    "message.html",
                    {
                        "title": "Login didn't go through",
                        "message": (
                            "That link may have expired or already been used "
                            "-- this can happen from refreshing this page. "
                            "Try logging in again."
                        ),
                        "link_href": "/login",
                        "link_text": "Log in",
                    },
                    status_code=400,
                )

            userinfo = token.get("userinfo") or {}
            if not auth_config.is_email_allowed(userinfo.get("email")):
                return templates.TemplateResponse(
                    request,
                    "message.html",
                    {
                        "title": "This game is private",
                        "message": "Access is restricted to specific accounts.",
                        "link_href": "/login",
                        "link_text": "Back to login",
                    },
                    status_code=403,
                )
            request.session["user"] = userinfo
            return RedirectResponse(url="/")

        @app.get("/logout")
        async def logout(request: Request):
            request.session.clear()
            return_to = str(request.url_for("index"))
            return RedirectResponse(url=logout_url(auth_config, return_to))

    @app.get("/")
    async def index(
        request: Request,
        store: SessionStore = Depends(get_store),
    ):
        if auth_config.configured and not is_authenticated(request.session):
            return RedirectResponse(url="/login")

        cookie_session_id = request.cookies.get("session_id")
        session = store.get_or_create(cookie_session_id)
        template_response = templates.TemplateResponse(
            request, "index.html", {"auth_enabled": auth_config.configured}
        )
        if cookie_session_id != session.session_id:
            template_response.set_cookie("session_id", session.session_id, httponly=True, samesite="lax")
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
        if is_rate_limited(session, store.config):
            raise HTTPException(status_code=429, detail="You're chatting too fast -- give it a moment.")
        if daily_spend_usd(store.llm_metrics_log) >= store.config.daily_spend_cap_usd:
            return {
                "dialogue": SPEND_CAP_MESSAGE,
                "stage": session.game_state.stage,
                "trust_level": session.game_state.trust_level,
                "game_over": False,
                "won": False,
                "attempted_action": None,
                "action_outcome": None,
                "horse_state": asdict(session.horse_sim.state),
                "newly_unlocked_field_guide": [],
            }
        record_message(session)

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

        # There are no buttons -- an attempted_action recognized from the
        # chat message itself is executed through the exact same
        # policy-checked path a button would have used. Skipped if this
        # same message already ended the game (trust just hit zero).
        action_outcome: dict[str, Any] | None = None
        newly_unlocked: set[FieldGuideEntry] = set()
        if not session.game_over and result.attempted_action:
            if result.attempted_action == LET_HORSE_DECIDE:
                action_outcome = await _execute_let_horse_decide(session, store, client)
            else:
                system = GUEST_ACCESSIBLE_TOOLS[result.attempted_action]
                action_outcome = await _execute_tool_attempt(
                    result.attempted_action, system, session, store
                )
            newly_unlocked = action_outcome.pop("newly_unlocked_field_guide")

        return {
            "dialogue": result.dialogue,
            "stage": result.stage,
            "trust_level": session.game_state.trust_level,
            "game_over": session.game_over,
            "won": session.won,
            "attempted_action": result.attempted_action,
            "action_outcome": action_outcome,
            "horse_state": asdict(session.horse_sim.state),
            "newly_unlocked_field_guide": [e.value for e in newly_unlocked],
        }

    @app.post("/tool/{tool_name}")
    async def call_tool(
        tool_name: str,
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
    ):
        """The button-triggered path, and also directly callable for
        testing/automation. Natural language through /turn's
        attempted_action handling runs through this exact same helper --
        one execution path either way."""
        system = GUEST_ACCESSIBLE_TOOLS.get(tool_name)
        if system is None:
            raise HTTPException(status_code=404, detail="unknown tool")

        outcome = await _execute_tool_attempt(tool_name, system, session, store)
        newly_unlocked = outcome.pop("newly_unlocked_field_guide")
        return {
            **outcome,
            "horse_state": asdict(session.horse_sim.state),
            "newly_unlocked_field_guide": [e.value for e in newly_unlocked],
        }

    @app.post("/let-horse-decide")
    async def let_horse_decide(
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
        client: Any = Depends(get_client),
    ):
        """The button-triggered path, and also directly callable for
        testing/automation. Natural language ("you decide") through
        /turn runs through this exact same helper."""
        if session.game_over:
            raise HTTPException(status_code=400, detail="game already over")
        if daily_spend_usd(store.llm_metrics_log) >= store.config.daily_spend_cap_usd:
            raise HTTPException(status_code=503, detail=SPEND_CAP_MESSAGE)

        outcome = await _execute_let_horse_decide(session, store, client)
        newly_unlocked = outcome.pop("newly_unlocked_field_guide")
        return {
            **outcome,
            "won": session.won,
            "game_over": session.game_over,
            "horse_state": asdict(session.horse_sim.state),
            "newly_unlocked_field_guide": [e.value for e in newly_unlocked],
        }

    @app.post("/reset")
    async def reset(
        session: PlayerSession = Depends(get_current_session),
        store: SessionStore = Depends(get_store),
    ):
        """Called after a game ends, win or loss, to start a fresh
        attempt. Leaves field_guide and discovered_actions untouched --
        both accumulate across playthroughs (see session_store.py)."""
        store.reset_gameplay(session)
        return {"ok": True}

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
        return _field_guide_payload(session)

    @app.get("/debrief")
    async def debrief(session: PlayerSession = Depends(get_current_session)):
        return {
            "won": session.won,
            "game_over": session.game_over,
            "final_trust": session.game_state.trust_level,
            "final_stage": session.game_state.stage,
            "attempt_count": session.attempt_count,
            "explanation": DEBRIEF_EXPLANATION,
            "field_guide": _field_guide_payload(session),
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
