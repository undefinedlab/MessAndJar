"""Mess&Jar envelope schema — protocol v1.

Separate-projects bet: bodies may carry specs/schemas/payloads;
artifacts are first-class via kind=artifact and refs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1
MAX_BODY_BYTES = 256 * 1024  # 256 KiB — sized for content, not novels


class MessKind(str, Enum):
    question = "question"  # wake receiver
    handoff = "handoff"  # transfer ownership / next step
    fyi = "fyi"  # queue until next session
    answer = "answer"  # reply to a question
    artifact = "artifact"  # substantive attachment pointer / inline payload


class AgentLocalState(BaseModel):
    """Per-agent state owned by a Jar."""

    agent_id: str
    workdir: str | None = None
    session_id: str | None = None
    hop: int = 0
    paused: bool = False
    last_seen_mess_id: str | None = None
    cursor: int = 0  # monotonic mess seq last delivered to this agent


class Jar(BaseModel):
    id: str
    name: str
    agents: list[str]
    local: dict[str, AgentLocalState] = Field(default_factory=dict)
    paused: bool = False
    archived: bool = False
    created_at: str
    meta: dict[str, Any] = Field(default_factory=dict)
    password: str | None = Field(default=None, exclude=True)

    @classmethod
    def create(
        cls,
        name: str,
        agents: list[str],
        meta: dict[str, Any] | None = None,
        password: str | None = None,
    ) -> Jar:
        from messjar.auth import generate_password

        agents = list(dict.fromkeys(a.strip() for a in agents if a.strip()))
        if len(agents) < 2:
            raise ValueError("a Jar needs at least two agents")
        now = _now()
        jar_id = f"jar_{uuid4().hex[:12]}"
        local = {a: AgentLocalState(agent_id=a) for a in agents}
        return cls(
            id=jar_id,
            name=name,
            agents=agents,
            local=local,
            created_at=now,
            meta=meta or {},
            password=(password.strip() if password and password.strip() else generate_password()),
        )

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class Mess(BaseModel):
    id: str
    jar_id: str
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    body: str
    kind: MessKind
    reply_expected: bool = False
    hop: int = 0
    refs: list[str] = Field(default_factory=list)
    ts: str
    schema_version: int = SCHEMA_VERSION
    seq: int | None = None  # assigned by store

    model_config = {"populate_by_name": True}

    @classmethod
    def create(
        cls,
        *,
        jar_id: str,
        from_agent: str,
        to_agent: str,
        body: str,
        kind: MessKind | str,
        reply_expected: bool | None = None,
        hop: int = 0,
        refs: list[str] | None = None,
    ) -> Mess:
        if isinstance(kind, str):
            kind = MessKind(kind)
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > MAX_BODY_BYTES:
            raise ValueError(f"body exceeds {MAX_BODY_BYTES} bytes")
        if reply_expected is None:
            reply_expected = kind in (MessKind.question, MessKind.handoff)
        return cls(
            id=f"msg_{uuid4().hex[:12]}",
            jar_id=jar_id,
            **{"from": from_agent, "to": to_agent},
            body=body,
            kind=kind,
            reply_expected=reply_expected,
            hop=hop,
            refs=refs or [],
            ts=_now(),
            schema_version=SCHEMA_VERSION,
        )

    def wakes_agent(self) -> bool:
        """Daemon: should this Mess spawn a tool session now?"""
        return self.kind in (MessKind.question, MessKind.handoff, MessKind.answer)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
