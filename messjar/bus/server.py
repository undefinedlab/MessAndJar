"""HTTP bus: REST + MCP + web UI (same Railway service)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from messjar.auth import (
    AuthContext,
    configured_password,
    extract_password,
    passwords_match,
)
from messjar.schema import AgentLocalState, Jar, Mess, MessKind
from messjar.share import agent_mcp_bundle, connection_bundle
from messjar.store import Store, database_host_label, normalize_database_url

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"


class CreateJarBody(BaseModel):
    name: str
    agents: list[str] = Field(default_factory=list)
    password: str | None = None
    repos: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class JoinBody(BaseModel):
    jar: str
    password: str
    tool: str  # cursor | claude | codex | opencode
    display_name: str | None = None


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


def _request_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="Mess&Jar", version="0.1.0")
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
        return templates.TemplateResponse(request, "share.html", {"error": None, **bundle})

    @app.post("/api/jars")
    def public_create_jar(request: Request, body: CreateJarBody) -> dict[str, Any]:
        try:
            jar = store.create_jar(
                body.name, body.agents, body.meta, password=body.password, repos=body.repos
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
                body.name, body.agents, body.meta, password=body.password, repos=body.repos
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

    @app.post("/send")
    def send_rest(body: SendBody, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        jar = pick_jar(
            auth,
            agent=body.from_agent,
            jar=body.jar,
            repo=body.repo,
            workdir=body.workdir,
        )
        assert jar is not None
        body.jar = jar.name
        return _send(store, body)

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
            ],
        }

    @app.get("/mcp")
    @app.get("/mcp/tools")
    def mcp_tools(auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        _ = auth
        return _mcp_tool_list()

    @app.post("/mcp/call")
    def mcp_call(
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

    return app


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
        )
        return store.send(mess).model_dump(by_alias=True)
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
