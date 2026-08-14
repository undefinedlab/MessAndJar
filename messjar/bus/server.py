"""HTTP bus: REST + MCP + web UI (same Railway service)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware import Middleware

from messjar.auth import (
    AuthContext,
    configured_password,
    extract_password,
    passwords_match,
)
from messjar import policy
from messjar.mcp_auth_asgi import mcp_auth_middleware
from messjar.mcp_protocol import build_mcp
from messjar.policy import ScopeViolation
from messjar.schema import AgentLocalState, Jar, LabelProposal, Mess, MessKind, label_diff
from messjar.share import agent_mcp_bundle, connection_bundle
from messjar.store import CircuitBreakerTripped, Store, database_host_label, normalize_database_url

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"


class CreateJarBody(BaseModel):
    name: str
    agents: list[str] = Field(default_factory=list)
    password: str | None = None
    repos: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    circuit: dict[str, int] | None = None


class JoinBody(BaseModel):
    jar: str
    password: str
    tool: str  # cursor | claude | codex | opencode
    display_name: str | None = None


class AddReposBody(BaseModel):
    jar: str
    password: str
    repos: list[str]


class SendBody(BaseModel):
    jar: str | None = None
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    body: str
    kind: MessKind = MessKind.fyi
    reply_expected: bool | None = None
    hop: int = 0
    refs: list[str] = Field(default_factory=list)
    repo: str | None = None
    workdir: str | None = None
    trigger_source: str = "human"

    model_config = {"populate_by_name": True}


class CheckBody(BaseModel):
    agent: str
    jar: str | None = None
    after_seq: int | None = None
    limit: int = 50
    ack: bool = True
    repo: str | None = None
    workdir: str | None = None


class WaitBody(BaseModel):
    agent: str
    jar: str | None = None
    after_seq: int | None = None
    timeout_s: float = 30.0
    repo: str | None = None
    workdir: str | None = None


class WhichJarBody(BaseModel):
    agent: str
    jar: str | None = None
    repo: str | None = None
    workdir: str | None = None


class ListJarsBody(BaseModel):
    agent: str | None = None
    include_archived: bool = False


class UpdateLabelBody(BaseModel):
    patch: str
    jar: str | None = None
    repo: str | None = None
    workdir: str | None = None
    agent: str | None = None
    origin_mess_id: str | None = None


class AckBody(BaseModel):
    jar: str
    agent: str
    seq: int


class LocalStateBody(BaseModel):
    jar: str | None = None
    agent: str
    workdir: str | None = None
    session_id: str | None = None
    hop: int | None = None
    paused: bool | None = None
    repo: str | None = None


class CircuitConfigBody(BaseModel):
    max_spawns: int | None = None
    window_s: int | None = None
    max_hop_depth: int | None = None


class PolicyConfigBody(BaseModel):
    held_kinds: list[str] | None = None


class HeldApproveBody(BaseModel):
    approved_by: str


class HeldRejectBody(BaseModel):
    rejected_by: str | None = None


class LabelProposeBody(BaseModel):
    agent: str
    patch: str
    origin_mess_id: str | None = None


class LabelDecideBody(BaseModel):
    agent: str
    decision: str  # accept | reject


class LabelEditBody(BaseModel):
    agent: str
    patch: str


class PublicLabelProposeBody(BaseModel):
    jar: str
    password: str
    agent: str
    patch: str
    origin_mess_id: str | None = None


class PublicLabelDecideBody(BaseModel):
    jar: str
    password: str
    proposal_id: str
    agent: str
    decision: str


class PublicLabelEditBody(BaseModel):
    jar: str
    password: str
    proposal_id: str
    agent: str
    patch: str


def _request_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def create_app(store: Store) -> FastAPI:
    mcp = build_mcp()
    # Real MCP for Cursor: Streamable HTTP at /mcp (Bearer agent key).
    # Legacy JSON for the Python daemon stays under /rpc/*.
    mcp_http = mcp.http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        transport="http",
        host_origin_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
        middleware=[Middleware(mcp_auth_middleware(store))],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_http.lifespan(app):
            yield

    app = FastAPI(title="Mess&Jar", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def resolve_auth(
        authorization: str | None = Header(default=None),
        x_messjar_password: str | None = Header(default=None, alias="X-MessJar-Password"),
    ) -> AuthContext:
        admin = configured_password()
        provided = extract_password(authorization, x_messjar_password)

        if not provided:
            if os.environ.get("MESSJAR_OPEN", "").lower() in ("1", "true", "yes"):
                return AuthContext(open_bus=True)
            raise HTTPException(401, "unauthorized: provide agent key or jar password")

        if admin and passwords_match(provided, admin):
            return AuthContext(admin=True)

        agent_id = store.get_agent_by_token(provided)
        if agent_id:
            return AuthContext(agent_id=agent_id)

        jar = store.get_jar_by_password(provided)
        if jar:
            return AuthContext(jar_id=jar.id, jar_name=jar.name)

        raise HTTPException(401, "unauthorized: bad or missing password")

    def require_jar_access(auth: AuthContext, jar: Jar) -> None:
        if not auth.allows_jar(jar.id, jar.name, jar.agents):
            raise HTTPException(403, "forbidden: not allowed on this jar")

    def pick_jar(
        auth: AuthContext,
        *,
        agent: str | None = None,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
        required: bool = True,
    ) -> Jar | None:
        agent_id = agent or auth.agent_id
        # jar-password auth forces that jar unless explicit override matches
        if auth.jar_name and not jar and not repo and not workdir:
            found = store.get_jar(auth.jar_id or auth.jar_name)
            if found:
                require_jar_access(auth, found)
                return found
        try:
            found = store.resolve_jar(
                agent_id=agent_id if not auth.admin else agent_id,
                jar=jar or auth.jar_name,
                repo=repo,
                workdir=workdir,
            )
        except (KeyError, ValueError, PermissionError) as e:
            if required:
                raise HTTPException(400, str(e)) from e
            return None
        require_jar_access(auth, found)
        return found

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "auth": "agent-key|jar|admin"}

    @app.get("/", response_class=HTMLResponse)
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/j/{jar_name}", response_class=HTMLResponse)
    def share_page(
        request: Request,
        jar_name: str,
        p: str | None = Query(default=None),
    ) -> HTMLResponse:
        jar = store.get_jar(jar_name)
        if not jar or not p or not jar.password or not passwords_match(p, jar.password):
            return templates.TemplateResponse(
                request,
                "share.html",
                {"jar": jar_name, "error": "Invalid share link or password.", "daemon": {}},
                status_code=401,
            )
        bundle = connection_bundle(
            base_url=_request_base(request),
            jar_name=jar.name,
            password=jar.password,
            agents=jar.agents,
            repos=jar.repos,
        )
        recent = [
            m.model_dump(by_alias=True)
            for m in store.list_messes(jar.id, after_seq=0, limit=30)
        ]
        # show newest first in UI
        recent = list(reversed(recent[-15:]))
        pending_proposals = [
            _proposal_public_dict(store, p) for p in store.list_label_proposals(jar.id, status="pending")
        ]
        return templates.TemplateResponse(
            request,
            "share.html",
            {
                "error": None,
                **bundle,
                "jar_id": jar.id,
                "created_at": jar.created_at,
                "paused": jar.paused,
                "recent": recent,
                "label": jar.label,
                "pending_proposals": pending_proposals,
            },
        )

    @app.get("/api/jars/{jar_name}/detail")
    def jar_detail_api(jar_name: str, p: str = Query(..., description="Jar password")) -> dict[str, Any]:
        jar = store.get_jar(jar_name)
        if not jar or not jar.password or not passwords_match(p, jar.password):
            raise HTTPException(401, "bad jar password")
        messes = store.list_messes(jar.id, after_seq=0, limit=50)
        return {
            "id": jar.id,
            "jar": jar.name,
            "agents": jar.agents,
            "repos": jar.repos,
            "paused": jar.paused,
            "created_at": jar.created_at,
            "mess_count": len(messes),
            "recent": [m.model_dump(by_alias=True) for m in messes[-20:]],
        }

    @app.post("/api/jars/repos")
    def add_jar_repos(body: AddReposBody) -> dict[str, Any]:
        jar = store.get_jar(body.jar)
        if not jar or not jar.password or not passwords_match(body.password, jar.password):
            raise HTTPException(401, "bad jar password")
        if not body.repos:
            raise HTTPException(400, "repos required")
        try:
            jar = store.add_repos(jar.id, body.repos)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"jar": jar.name, "repos": jar.repos, "agents": jar.agents}

    def _public_jar_or_401(jar_name: str, password: str) -> Jar:
        jar = store.get_jar(jar_name)
        if not jar or not jar.password or not passwords_match(password, jar.password):
            raise HTTPException(401, "bad jar password")
        return jar

    @app.post("/api/jars/label/propose")
    def public_propose_label(body: PublicLabelProposeBody) -> dict[str, Any]:
        jar = _public_jar_or_401(body.jar, body.password)
        try:
            proposal = store.propose_label(
                jar.id, proposed_by=body.agent, patch=body.patch, origin_mess_id=body.origin_mess_id
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.get("/api/jars/label/proposals")
    def public_list_label_proposals(
        jar: str, p: str = Query(...), status: str = "pending"
    ) -> list[dict[str, Any]]:
        found = _public_jar_or_401(jar, p)
        proposals = store.list_label_proposals(found.id, status=status or None)
        return [_proposal_public_dict(store, prop) for prop in proposals]

    @app.post("/api/jars/label/decide")
    def public_decide_label(body: PublicLabelDecideBody) -> dict[str, Any]:
        _public_jar_or_401(body.jar, body.password)
        try:
            proposal = store.decide_label_proposal(
                body.proposal_id, participant=body.agent, decision=body.decision, client="web"
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.post("/api/jars/label/edit")
    def public_edit_label(body: PublicLabelEditBody) -> dict[str, Any]:
        _public_jar_or_401(body.jar, body.password)
        try:
            proposal = store.edit_label_proposal(
                body.proposal_id, participant=body.agent, patch=body.patch, client="web"
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.post("/api/jars")
    def public_create_jar(request: Request, body: CreateJarBody) -> dict[str, Any]:
        try:
            jar = store.create_jar(
                body.name,
                body.agents,
                body.meta,
                password=body.password,
                repos=body.repos,
                circuit=body.circuit,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        assert jar.password
        bundle = connection_bundle(
            base_url=_request_base(request),
            jar_name=jar.name,
            password=jar.password,
            agents=jar.agents,
            repos=jar.repos,
        )
        return {
            "id": jar.id,
            "jar": jar.name,
            "agents": jar.agents,
            "repos": jar.repos,
            "password": jar.password,
            "share_url": bundle["share_url"],
            "bundle": bundle,
        }

    @app.post("/api/agent-key")
    @app.post("/api/join")
    def join_jar(request: Request, body: JoinBody) -> dict[str, Any]:
        """Join with tool + optional name → auto agent id, attach to jar, return MCP key."""
        from messjar.agents import make_agent_id, normalize_tool

        jar = store.get_jar(body.jar)
        if not jar or not jar.password or not passwords_match(body.password, jar.password):
            raise HTTPException(401, "bad jar password")
        try:
            tool = normalize_tool(body.tool)
            agent_id = make_agent_id(body.display_name, tool)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

        jar = store.attach_agent(jar.id, agent_id)
        _, token = store.upsert_agent_key(agent_id)
        return {
            "agent_id": agent_id,
            "tool": tool,
            "jar": jar.name,
            "agents": jar.agents,
            "token": token,
            **agent_mcp_bundle(
                base_url=_request_base(request), agent_id=agent_id, token=token
            ),
        }

    @app.post("/jars")
    def create_jar(
        request: Request,
        body: CreateJarBody,
        auth: AuthContext = Depends(resolve_auth),
    ) -> dict[str, Any]:
        if not (auth.admin or auth.open_bus):
            raise HTTPException(403, "admin password required to create via API (or use /api/jars)")
        try:
            jar = store.create_jar(
                body.name,
                body.agents,
                body.meta,
                password=body.password,
                repos=body.repos,
                circuit=body.circuit,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        data = jar.public_dict()
        if jar.password:
            data["password"] = jar.password
            data["share_url"] = connection_bundle(
                base_url=_request_base(request),
                jar_name=jar.name,
                password=jar.password,
                agents=jar.agents,
                repos=jar.repos,
            )["share_url"]
        return data

    @app.get("/jars")
    def list_jars(
        agent: str | None = None,
        include_archived: bool = False,
        auth: AuthContext = Depends(resolve_auth),
    ) -> list[dict[str, Any]]:
        if auth.admin or auth.open_bus:
            jars = store.list_jars(agent, include_archived)
        elif auth.agent_id:
            jars = store.list_jars(auth.agent_id, include_archived)
        else:
            j = store.get_jar(auth.jar_id or "")
            jars = [j] if j and (not agent or agent in j.agents) else []
        return [j.public_dict() for j in jars if j]

    @app.get("/jars/{jar}")
    def get_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        return j.public_dict()

    @app.post("/jars/{jar}/pause")
    def pause_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        return store.set_jar_paused(jar, True).public_dict()

    @app.post("/jars/{jar}/resume")
    def resume_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        return store.set_jar_paused(jar, False).public_dict()

    @app.post("/jars/{jar}/circuit")
    def set_jar_circuit(
        jar: str, body: CircuitConfigBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        cfg = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            return store.set_jar_circuit(jar, cfg).public_dict()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/jars/{jar}/policy")
    def set_jar_policy(
        jar: str, body: PolicyConfigBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        cfg = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            return store.set_jar_policy(jar, cfg).public_dict()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/jars/{jar}/held")
    def list_held(
        jar: str, from_agent: str | None = None, auth: AuthContext = Depends(resolve_auth)
    ) -> list[dict[str, Any]]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        return [h.model_dump() for h in store.list_held_messages(jar, from_agent=from_agent)]

    @app.post("/jars/{jar}/held/{held_id}/approve")
    def approve_held(
        jar: str, held_id: str, body: HeldApproveBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        try:
            return store.approve_held_message(
                held_id, approved_by=body.approved_by, client="api"
            ).model_dump(by_alias=True)
        except CircuitBreakerTripped as e:
            raise HTTPException(429, str(e)) from e
        except ScopeViolation as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/jars/{jar}/held/{held_id}/reject")
    def reject_held(
        jar: str, held_id: str, body: HeldRejectBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        try:
            return store.reject_held_message(
                held_id, rejected_by=body.rejected_by, client="api"
            ).model_dump()
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/jars/{jar}/label/propose")
    def propose_label(
        jar: str, body: LabelProposeBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        try:
            proposal = store.propose_label(
                jar, proposed_by=body.agent, patch=body.patch, origin_mess_id=body.origin_mess_id
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.get("/jars/{jar}/label/proposals")
    def list_label_proposals(
        jar: str, status: str = "pending", auth: AuthContext = Depends(resolve_auth)
    ) -> list[dict[str, Any]]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        proposals = store.list_label_proposals(jar, status=status or None)
        return [_proposal_public_dict(store, p) for p in proposals]

    @app.post("/jars/{jar}/label/proposals/{proposal_id}/decide")
    def decide_label_proposal(
        jar: str,
        proposal_id: str,
        body: LabelDecideBody,
        auth: AuthContext = Depends(resolve_auth),
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        try:
            proposal = store.decide_label_proposal(
                proposal_id, participant=body.agent, decision=body.decision, client="cli"
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.post("/jars/{jar}/label/proposals/{proposal_id}/edit")
    def edit_label_proposal(
        jar: str,
        proposal_id: str,
        body: LabelEditBody,
        auth: AuthContext = Depends(resolve_auth),
    ) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        try:
            proposal = store.edit_label_proposal(
                proposal_id, participant=body.agent, patch=body.patch, client="cli"
            )
        except (KeyError, PermissionError, ValueError) as e:
            raise _label_error(e) from e
        return _proposal_public_dict(store, proposal)

    @app.get("/jars/{jar}/messes")
    def jar_messes(
        jar: str,
        after_seq: int = 0,
        limit: int = 100,
        auth: AuthContext = Depends(resolve_auth),
    ) -> list[dict[str, Any]]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j)
        return [
            m.model_dump(by_alias=True)
            for m in store.list_messes(jar, after_seq=after_seq, limit=limit)
        ]

    @app.get("/digest")
    def digest(
        jar: str | None = None,
        agent: str | None = None,
        auth: AuthContext = Depends(resolve_auth),
    ) -> list[dict[str, Any]]:
        """'What am I blocked on?' — one call, across every jar by default."""
        if jar:
            found = store.get_jar(jar)
            if not found:
                raise HTTPException(404, "jar not found")
            require_jar_access(auth, found)
            effective_agent = agent or (None if (auth.admin or auth.open_bus) else auth.agent_id)
            return store.digest(agent_id=effective_agent, jar_id_or_name=found.id)
        if auth.admin or auth.open_bus:
            return store.digest(agent_id=agent, jar_id_or_name=None)
        if auth.agent_id:
            return store.digest(agent_id=agent or auth.agent_id, jar_id_or_name=None)
        # jar-password auth with no explicit jar= — scope to that one jar
        found = store.get_jar(auth.jar_id or "")
        if not found:
            return []
        return store.digest(agent_id=agent, jar_id_or_name=found.id)

    @app.post("/send")
    def send_rest(
        body: SendBody, response: Response, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        jar = pick_jar(
            auth,
            agent=body.from_agent,
            jar=body.jar,
            repo=body.repo,
            workdir=body.workdir,
        )
        assert jar is not None
        body.jar = jar.name
        result = _send(store, body)
        if result.get("status") == "held":
            response.status_code = 202  # queued for the sender's human, not delivered
        return result

    @app.post("/ack")
    def ack(body: AckBody, auth: AuthContext = Depends(resolve_auth)) -> dict[str, str]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, f"jar not found: {body.jar}")
        require_jar_access(auth, j)
        try:
            store.advance_cursor(j.id, body.agent, body.seq)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        return {"status": "ok"}

    @app.post("/local")
    def update_local(
        body: LocalStateBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = pick_jar(
            auth, agent=body.agent, jar=body.jar, repo=body.repo, workdir=body.workdir
        )
        assert j is not None
        cur = j.local.get(body.agent) or AgentLocalState(agent_id=body.agent)
        if body.workdir is not None:
            cur.workdir = body.workdir
        if body.session_id is not None:
            cur.session_id = body.session_id
        if body.hop is not None:
            cur.hop = body.hop
        if body.paused is not None:
            cur.paused = body.paused
        try:
            return store.update_agent_local(j.id, cur).public_dict()
        except KeyError as e:
            raise HTTPException(400, str(e)) from e

    def _mcp_tool_list() -> dict[str, Any]:
        return {
            "name": "messjar",
            "version": "0.1.0",
            "description": (
                "Mess&Jar peer agent bus. One MCP connection; pass workdir or repo "
                "and the bus picks the jar for the project you're in."
            ),
            "tools": [
                {
                    "name": "which_jar",
                    "description": "Resolve which jar matches this agent + workdir/repo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "jar": {"type": "string"},
                            "repo": {"type": "string"},
                            "workdir": {"type": "string"},
                        },
                        "required": ["agent"],
                    },
                },
                {
                    "name": "send",
                    "description": "Post a Mess (jar optional if workdir/repo bound)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "jar": {"type": "string"},
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "body": {"type": "string"},
                            "kind": {"type": "string", "enum": [k.value for k in MessKind]},
                            "reply_expected": {"type": "boolean"},
                            "hop": {"type": "integer"},
                            "refs": {"type": "array", "items": {"type": "string"}},
                            "repo": {"type": "string"},
                            "workdir": {"type": "string"},
                        },
                        "required": ["from", "to", "body", "kind"],
                    },
                },
                {
                    "name": "check_jar",
                    "description": "New messes for this agent (scoped by jar or workdir/repo)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "jar": {"type": "string"},
                            "repo": {"type": "string"},
                            "workdir": {"type": "string"},
                            "after_seq": {"type": "integer"},
                            "limit": {"type": "integer"},
                            "ack": {"type": "boolean"},
                        },
                        "required": ["agent"],
                    },
                },
                {
                    "name": "wait",
                    "description": "Block until a Mess arrives",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "jar": {"type": "string"},
                            "repo": {"type": "string"},
                            "workdir": {"type": "string"},
                            "after_seq": {"type": "integer"},
                            "timeout_s": {"type": "number"},
                        },
                        "required": ["agent"],
                    },
                },
                {
                    "name": "list_jars",
                    "description": "Jars this agent is attached to",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "include_archived": {"type": "boolean"},
                        },
                    },
                },
                {
                    "name": "update_label",
                    "description": (
                        "Propose a change to the jar's label (persistent shared context). "
                        "Creates a pending proposal; does not write until every participant "
                        "accepts it via the web UI or `mj label accept`."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "patch": {"type": "string"},
                            "jar": {"type": "string"},
                            "repo": {"type": "string"},
                            "workdir": {"type": "string"},
                            "agent": {"type": "string"},
                            "origin_mess_id": {"type": "string"},
                        },
                        "required": ["patch"],
                    },
                },
            ],
        }

    @app.get("/rpc/tools")
    def rpc_tools(auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        _ = auth
        return _mcp_tool_list()

    @app.post("/rpc/call")
    def rpc_call(
        payload: dict[str, Any], auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        name = payload.get("name")
        args = payload.get("arguments") or {}
        try:
            if name == "which_jar":
                body = WhichJarBody.model_validate(args)
                jar = pick_jar(
                    auth,
                    agent=body.agent,
                    jar=body.jar,
                    repo=body.repo,
                    workdir=body.workdir,
                )
                assert jar is not None
                return {
                    "content": [
                        {
                            "type": "json",
                            "json": {
                                "jar": jar.name,
                                "id": jar.id,
                                "repos": jar.repos,
                                "agents": jar.agents,
                            },
                        }
                    ]
                }
            if name == "send":
                body = SendBody.model_validate(args)
                jar = pick_jar(
                    auth,
                    agent=body.from_agent,
                    jar=body.jar,
                    repo=body.repo,
                    workdir=body.workdir,
                )
                assert jar is not None
                body.jar = jar.name
                return {"content": [{"type": "json", "json": _send(store, body)}]}
            if name == "check_jar":
                body = CheckBody.model_validate(args)
                jar_filter = body.jar
                if body.repo or body.workdir or (not jar_filter and auth.jar_name):
                    jar = pick_jar(
                        auth,
                        agent=body.agent,
                        jar=body.jar,
                        repo=body.repo,
                        workdir=body.workdir,
                        required=bool(body.repo or body.workdir or auth.jar_name),
                    )
                    if jar:
                        jar_filter = jar.name
                if auth.agent_id and not jar_filter:
                    # all jars for this agent
                    jar_filter = None
                elif auth.jar_name and not jar_filter:
                    jar_filter = auth.jar_name
                batch = store.check_jar(
                    body.agent, jar_filter, after_seq=body.after_seq, limit=body.limit
                )
                if body.ack:
                    for item in batch:
                        j = store.get_jar(item["jar"]["id"])
                        if j:
                            require_jar_access(auth, j)
                        store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "wait":
                body = WaitBody.model_validate(args)
                jar_filter = body.jar
                if body.repo or body.workdir or auth.jar_name:
                    jar = pick_jar(
                        auth,
                        agent=body.agent,
                        jar=body.jar,
                        repo=body.repo,
                        workdir=body.workdir,
                    )
                    if jar:
                        jar_filter = jar.name
                batch = store.wait(
                    body.agent,
                    jar_id_or_name=jar_filter,
                    after_seq=body.after_seq,
                    timeout_s=body.timeout_s,
                )
                for item in batch:
                    j = store.get_jar(item["jar"]["id"])
                    if j:
                        require_jar_access(auth, j)
                    store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "list_jars":
                body = ListJarsBody.model_validate(args)
                agent = body.agent or auth.agent_id
                if auth.admin or auth.open_bus:
                    jars = store.list_jars(agent, body.include_archived)
                elif auth.agent_id:
                    jars = store.list_jars(auth.agent_id, body.include_archived)
                else:
                    j = store.get_jar(auth.jar_id or "")
                    jars = [j] if j else []
                return {
                    "content": [
                        {"type": "json", "json": [j.public_dict() for j in jars if j]}
                    ]
                }
            if name == "update_label":
                body = UpdateLabelBody.model_validate(args)
                jar = pick_jar(
                    auth,
                    agent=body.agent,
                    jar=body.jar,
                    repo=body.repo,
                    workdir=body.workdir,
                )
                assert jar is not None
                proposed_by = body.agent or auth.agent_id
                if not proposed_by:
                    raise HTTPException(400, "update_label requires an agent id")
                try:
                    proposal = store.propose_label(
                        jar.id,
                        proposed_by=proposed_by,
                        patch=body.patch,
                        origin_mess_id=body.origin_mess_id,
                    )
                except (KeyError, PermissionError, ValueError) as e:
                    raise _label_error(e) from e
                return {"content": [{"type": "json", "json": _proposal_public_dict(store, proposal)}]}
            raise HTTPException(400, f"unknown tool: {name}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # Catch-all after REST routes: Streamable HTTP MCP lives at /mcp
    app.mount("/", mcp_http)

    return app


def _label_error(e: Exception) -> HTTPException:
    if isinstance(e, KeyError):
        return HTTPException(404, str(e))
    if isinstance(e, PermissionError):
        return HTTPException(403, str(e))
    return HTTPException(400, str(e))


def _proposal_public_dict(store: Store, proposal: LabelProposal) -> dict[str, Any]:
    data = proposal.model_dump()
    data["accepted_by"] = [
        a["participant"] for a in store.list_approvals(proposal.id) if a["decision"] == "accept"
    ]
    data["diff"] = label_diff(proposal.base_label, proposal.patch)
    data["origin_mess"] = None
    if proposal.origin_mess_id:
        origin = store.get_mess(proposal.origin_mess_id)
        if origin:
            data["origin_mess"] = origin.model_dump(by_alias=True)
    return data


def _send(store: Store, body: SendBody) -> dict[str, Any]:
    jar = store.get_jar(body.jar or "")
    if not jar:
        raise HTTPException(404, f"jar not found: {body.jar}")
    try:
        mess = Mess.create(
            jar_id=jar.id,
            from_agent=body.from_agent,
            to_agent=body.to_agent,
            body=body.body,
            kind=body.kind,
            reply_expected=body.reply_expected,
            hop=body.hop,
            refs=body.refs,
            trigger_source=body.trigger_source,
        )
        result = policy.send(store, mess, client="api")
        return policy.public_dict(result)
    except CircuitBreakerTripped as e:
        raise HTTPException(429, str(e)) from e
    except ScopeViolation as e:
        raise HTTPException(403, str(e)) from e
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


def run_server(
    database_url: str | None = None,
    host: str = "0.0.0.0",
    port: int = 7420,
) -> None:
    import uvicorn

    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required (PostgreSQL connection string)")

    store = Store(normalize_database_url(url))
    app = create_app(store)
    label = database_host_label(url)
    auth_state = "admin+agent+jar" if configured_password() else "agent+jar"
    print(f"Mess&Jar bus postgres={label} auth={auth_state} http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
