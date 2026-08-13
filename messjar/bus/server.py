"""HTTP bus with REST + MCP-shaped tool endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from messjar.schema import Mess, MessKind
from messjar.store import Store


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
    ack: bool = True  # advance cursor after delivery


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


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="Mess&Jar Bus", version="0.1.0")
    app.state.store = store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- human / CLI REST ---

    @app.post("/jars")
    def create_jar(body: CreateJarBody) -> dict[str, Any]:
        try:
            jar = store.create_jar(body.name, body.agents, body.meta)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"jar name already exists: {body.name}") from e
            raise
        return jar.model_dump(by_alias=True)

    @app.get("/jars")
    def list_jars(agent: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        return [j.model_dump(by_alias=True) for j in store.list_jars(agent, include_archived)]

    @app.get("/jars/{jar}")
    def get_jar(jar: str) -> dict[str, Any]:
        j = store.get_jar(jar)
        if not j:
            raise HTTPException(404, "jar not found")
        return j.model_dump(by_alias=True)

    @app.post("/jars/{jar}/pause")
    def pause_jar(jar: str) -> dict[str, Any]:
        try:
            return store.set_jar_paused(jar, True).model_dump(by_alias=True)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/jars/{jar}/resume")
    def resume_jar(jar: str) -> dict[str, Any]:
        try:
            return store.set_jar_paused(jar, False).model_dump(by_alias=True)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/jars/{jar}/messes")
    def jar_messes(jar: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return [m.model_dump(by_alias=True) for m in store.list_messes(jar, after_seq=after_seq, limit=limit)]
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/send")
    def send_rest(body: SendBody) -> dict[str, Any]:
        return _send(store, body)

    @app.post("/ack")
    def ack(body: AckBody) -> dict[str, str]:
        try:
            store.advance_cursor(body.jar if body.jar.startswith("jar_") else _resolve_jar_id(store, body.jar), body.agent, body.seq)
        except KeyError as e:
            # try resolve name
            j = store.get_jar(body.jar)
            if not j:
                raise HTTPException(404, str(e)) from e
            store.advance_cursor(j.id, body.agent, body.seq)
        return {"status": "ok"}

    @app.post("/local")
    def update_local(body: LocalStateBody) -> dict[str, Any]:
        j = store.get_jar(body.jar)
        if not j:
            raise HTTPException(404, "jar not found")
        from messjar.schema import AgentLocalState

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

    # --- MCP-shaped tools (JSON-RPC-ish HTTP) ---

    @app.get("/mcp/tools")
    def mcp_tools() -> dict[str, Any]:
        return {
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
            ]
        }

    @app.post("/mcp/call")
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
                        jar_id = item["jar"]["id"]
                        seq = item["cursor"]
                        store.advance_cursor(jar_id, body.agent, seq)
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

    return app


def _resolve_jar_id(store: Store, jar: str) -> str:
    j = store.get_jar(jar)
    if not j:
        raise KeyError(f"jar not found: {jar}")
    return j.id


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


def run_server(db: str | Path, host: str = "127.0.0.1", port: int = 7420) -> None:
    import uvicorn

    store = Store(db)
    app = create_app(store)
    uvicorn.run(app, host=host, port=port, log_level="info")
