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
from messjar.schema import AgentLocalState, Mess, MessKind
from messjar.share import connection_bundle
from messjar.store import Store, database_host_label, normalize_database_url

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"


class CreateJarBody(BaseModel):
    name: str
    agents: list[str]
    password: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SendBody(BaseModel):
    jar: str
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    body: str
    kind: MessKind = MessKind.fyi
    reply_expected: bool | None = None
    hop: int = 0
    refs: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class CheckBody(BaseModel):
    agent: str
    jar: str | None = None
    after_seq: int | None = None
    limit: int = 50
    ack: bool = True


class WaitBody(BaseModel):
    agent: str
    jar: str | None = None
    after_seq: int | None = None
    timeout_s: float = 30.0


class ListJarsBody(BaseModel):
    agent: str | None = None
    include_archived: bool = False


class AckBody(BaseModel):
    jar: str
    agent: str
    seq: int


class LocalStateBody(BaseModel):
    jar: str
    agent: str
    workdir: str | None = None
    session_id: str | None = None
    hop: int | None = None
    paused: bool | None = None


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

        if not admin:
            # No admin secret: jar passwords still work; bare open only if also no jar pw provided
            if not provided:
                # Allow open only when explicitly enabled
                if os.environ.get("MESSJAR_OPEN", "").lower() in ("1", "true", "yes"):
                    return AuthContext(open_bus=True)
                raise HTTPException(401, "unauthorized: provide jar password")
            jar = store.get_jar_by_password(provided)
            if jar:
                return AuthContext(jar_id=jar.id, jar_name=jar.name)
            raise HTTPException(401, "unauthorized: bad or missing password")

        if not provided:
            raise HTTPException(401, "unauthorized: bad or missing password")
        if passwords_match(provided, admin):
            return AuthContext(admin=True)
        jar = store.get_jar_by_password(provided)
        if jar:
            return AuthContext(jar_id=jar.id, jar_name=jar.name)
        raise HTTPException(401, "unauthorized: bad or missing password")

    def require_jar_access(auth: AuthContext, jar_id: str, jar_name: str | None = None) -> None:
        if not auth.allows_jar(jar_id, jar_name):
            raise HTTPException(403, "forbidden: password is not for this jar")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "auth": "jar-or-admin" if configured_password() else "jar",
        }

    @app.get("/", response_class=HTMLResponse)
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/j/{jar_name}", response_class=HTMLResponse)
    def share_page(
        request: Request,
        jar_name: str,
        p: str | None = Query(default=None, description="Jar password"),
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
        )
        return templates.TemplateResponse(request, "share.html", {"error": None, **bundle})

    @app.post("/api/jars")
    def public_create_jar(request: Request, body: CreateJarBody) -> dict[str, Any]:
        """Create a jar from the website — returns share link + password."""
        try:
            jar = store.create_jar(body.name, body.agents, body.meta, password=body.password)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        assert jar.password
        bundle = connection_bundle(
            base_url=_request_base(request),
            jar_name=jar.name,
            password=jar.password,
            agents=jar.agents,
        )
        return {
            "id": jar.id,
            "jar": jar.name,
            "agents": jar.agents,
            "password": jar.password,
            "share_url": bundle["share_url"],
            "bundle": bundle,
        }

    @app.post("/jars")
    def create_jar(
        request: Request,
        body: CreateJarBody,
        auth: AuthContext = Depends(resolve_auth),
    ) -> dict[str, Any]:
        if not (auth.admin or auth.open_bus):
            # jar-scoped tokens cannot create new jars
            raise HTTPException(403, "admin password required to create via API (or use /api/jars)")
        try:
            jar = store.create_jar(body.name, body.agents, body.meta, password=body.password)
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
        else:
            j = store.get_jar(auth.jar_id or "")
            jars = [j] if j and (not agent or agent in j.agents) else []
        return [j.public_dict() for j in jars if j]

    @app.get("/jars/{jar}")
    def get_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j.id, j.name)
        return j.public_dict()

    @app.post("/jars/{jar}/pause")
    def pause_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j.id, j.name)
        return store.set_jar_paused(jar, True).public_dict()

    @app.post("/jars/{jar}/resume")
    def resume_jar(jar: str, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j.id, j.name)
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
        require_jar_access(auth, j.id, j.name)
        return [
            m.model_dump(by_alias=True)
            for m in store.list_messes(jar, after_seq=after_seq, limit=limit)
        ]

    @app.post("/send")
    def send_rest(body: SendBody, auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        jar = store.get_jar(body.jar)
        if not jar:
            raise HTTPException(404, f"jar not found: {body.jar}")
        require_jar_access(auth, jar.id, jar.name)
        return _send(store, body)

    @app.post("/ack")
    def ack(body: AckBody, auth: AuthContext = Depends(resolve_auth)) -> dict[str, str]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, f"jar not found: {body.jar}")
        require_jar_access(auth, j.id, j.name)
        try:
            store.advance_cursor(j.id, body.agent, body.seq)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        return {"status": "ok"}

    @app.post("/local")
    def update_local(
        body: LocalStateBody, auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, "jar not found")
        require_jar_access(auth, j.id, j.name)
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

    @app.get("/mcp")
    @app.get("/mcp/tools")
    def mcp_tools(auth: AuthContext = Depends(resolve_auth)) -> dict[str, Any]:
        _ = auth
        return {
            "name": "messjar",
            "version": "0.1.0",
            "description": "Mess&Jar peer agent bus",
            "tools": [
                {
                    "name": "send",
                    "description": "Post a Mess into a Jar",
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
                        },
                        "required": ["jar", "from", "to", "body", "kind"],
                    },
                },
                {
                    "name": "check_jar",
                    "description": "Non-blocking: new messes for this agent",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "jar": {"type": "string"},
                            "after_seq": {"type": "integer"},
                            "limit": {"type": "integer"},
                            "ack": {"type": "boolean"},
                        },
                        "required": ["agent"],
                    },
                },
                {
                    "name": "wait",
                    "description": "Block until a matching Mess arrives or timeout",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "jar": {"type": "string"},
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

    @app.post("/mcp/call")
    def mcp_call(
        payload: dict[str, Any], auth: AuthContext = Depends(resolve_auth)
    ) -> dict[str, Any]:
        name = payload.get("name")
        args = payload.get("arguments") or {}
        try:
            if name == "send":
                body = SendBody.model_validate(args)
                jar = store.get_jar(body.jar)
                if not jar:
                    raise HTTPException(404, f"jar not found: {body.jar}")
                require_jar_access(auth, jar.id, jar.name)
                return {"content": [{"type": "json", "json": _send(store, body)}]}
            if name == "check_jar":
                body = CheckBody.model_validate(args)
                jar_filter = body.jar
                if not auth.admin and not auth.open_bus:
                    jar_filter = auth.jar_name
                batch = store.check_jar(
                    body.agent, jar_filter, after_seq=body.after_seq, limit=body.limit
                )
                if body.ack:
                    for item in batch:
                        require_jar_access(auth, item["jar"]["id"], item["jar"]["name"])
                        store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "wait":
                body = WaitBody.model_validate(args)
                jar_filter = body.jar
                if not auth.admin and not auth.open_bus:
                    jar_filter = auth.jar_name
                batch = store.wait(
                    body.agent,
                    jar_id_or_name=jar_filter,
                    after_seq=body.after_seq,
                    timeout_s=body.timeout_s,
                )
                for item in batch:
                    require_jar_access(auth, item["jar"]["id"], item["jar"]["name"])
                    store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "list_jars":
                body = ListJarsBody.model_validate(args)
                if auth.admin or auth.open_bus:
                    jars = store.list_jars(body.agent, body.include_archived)
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
    jar = store.get_jar(body.jar)
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
    auth_state = "admin+jar" if configured_password() else "jar-passwords"
    print(f"Mess&Jar bus postgres={label} auth={auth_state} http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
