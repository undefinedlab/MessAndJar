"""HTTP bus with REST + MCP-shaped tool endpoints."""

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from messjar.auth import configured_password, extract_password, passwords_match
from messjar.schema import AgentLocalState, Mess, MessKind
from messjar.store import Store, database_host_label, normalize_database_url


class CreateJarBody(BaseModel):
    name: str
    agents: list[str]
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


def require_auth(
    authorization: str | None = Header(default=None),
    x_messjar_password: str | None = Header(default=None, alias="X-MessJar-Password"),
) -> None:
    expected = configured_password()
    if not expected:
        # Local/dev convenience: no password configured → open bus.
        return
    provided = extract_password(authorization, x_messjar_password)
    if not passwords_match(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized: bad or missing password")


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="Mess&Jar Bus", version="0.1.0")
    app.state.store = store
    auth: Callable[..., Any] = require_auth

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "auth": "required" if configured_password() else "open"}

    @app.post("/jars", dependencies=[Depends(auth)])
    def create_jar(body: CreateJarBody) -> dict[str, Any]:
        try:
            jar = store.create_jar(body.name, body.agents, body.meta)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return jar.model_dump(by_alias=True)

    @app.get("/jars", dependencies=[Depends(auth)])
    def list_jars(agent: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        return [j.model_dump(by_alias=True) for j in store.list_jars(agent, include_archived)]

    @app.get("/jars/{jar}", dependencies=[Depends(auth)])
    def get_jar(jar: str) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        return j.model_dump(by_alias=True)

    @app.post("/jars/{jar}/pause", dependencies=[Depends(auth)])
    def pause_jar(jar: str) -> dict[str, Any]:
        try:
            return store.set_jar_paused(jar, True).model_dump(by_alias=True)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/jars/{jar}/resume", dependencies=[Depends(auth)])
    def resume_jar(jar: str) -> dict[str, Any]:
        try:
            return store.set_jar_paused(jar, False).model_dump(by_alias=True)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/jars/{jar}/messes", dependencies=[Depends(auth)])
    def jar_messes(jar: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return [
                m.model_dump(by_alias=True)
                for m in store.list_messes(jar, after_seq=after_seq, limit=limit)
            ]
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/send", dependencies=[Depends(auth)])
    def send_rest(body: SendBody) -> dict[str, Any]:
        return _send(store, body)

    @app.post("/ack", dependencies=[Depends(auth)])
    def ack(body: AckBody) -> dict[str, str]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, f"jar not found: {body.jar}")
        try:
            store.advance_cursor(j.id, body.agent, body.seq)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        return {"status": "ok"}

    @app.post("/local", dependencies=[Depends(auth)])
    def update_local(body: LocalStateBody) -> dict[str, Any]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, "jar not found")
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
            return store.update_agent_local(j.id, cur).model_dump(by_alias=True)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/mcp", dependencies=[Depends(auth)])
    @app.get("/mcp/tools", dependencies=[Depends(auth)])
    def mcp_tools() -> dict[str, Any]:
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
                            "kind": {
                                "type": "string",
                                "enum": [k.value for k in MessKind],
                            },
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

    @app.post("/mcp/call", dependencies=[Depends(auth)])
    def mcp_call(payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        args = payload.get("arguments") or {}
        try:
            if name == "send":
                body = SendBody.model_validate(args)
                return {"content": [{"type": "json", "json": _send(store, body)}]}
            if name == "check_jar":
                body = CheckBody.model_validate(args)
                batch = store.check_jar(
                    body.agent, body.jar, after_seq=body.after_seq, limit=body.limit
                )
                if body.ack:
                    for item in batch:
                        store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "wait":
                body = WaitBody.model_validate(args)
                batch = store.wait(
                    body.agent,
                    jar_id_or_name=body.jar,
                    after_seq=body.after_seq,
                    timeout_s=body.timeout_s,
                )
                for item in batch:
                    store.advance_cursor(item["jar"]["id"], body.agent, item["cursor"])
                return {"content": [{"type": "json", "json": batch}]}
            if name == "list_jars":
                body = ListJarsBody.model_validate(args)
                jars = store.list_jars(body.agent, body.include_archived)
                return {
                    "content": [
                        {
                            "type": "json",
                            "json": [j.model_dump(by_alias=True) for j in jars],
                        }
                    ]
                }
            raise HTTPException(400, f"unknown tool: {name}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @app.middleware("http")
    async def force_https_hint(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # Help reverse proxies / clients know this bus expects https in prod.
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
    if not configured_password() and os.environ.get("MESSJAR_REQUIRE_AUTH", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        raise SystemExit("MESSJAR_PASSWORD is required when MESSJAR_REQUIRE_AUTH=1")

    store = Store(normalize_database_url(url))
    app = create_app(store)
    label = database_host_label(url)
    auth_state = "on" if configured_password() else "OFF (set MESSJAR_PASSWORD)"
    print(f"Mess&Jar bus postgres={label} auth={auth_state} http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
