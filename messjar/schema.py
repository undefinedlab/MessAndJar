"""Mess&Jar envelope schema — protocol v1.

Separate-projects bet: bodies may carry specs/schemas/payloads;
artifacts are first-class via kind=artifact and refs.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from messjar.refs import has_verifiable_ref

SCHEMA_VERSION = 1
MAX_BODY_BYTES = 256 * 1024  # 256 KiB — sized for content, not novels
LABEL_MAX_BYTES = 2048  # ~2KB — a summary, not a spec dump
LOOP_TIMEOUT_S = 24 * 60 * 60  # a loop with no answer in a day surfaces as stale


class MessKind(str, Enum):
    question = "question"  # wake receiver
    handoff = "handoff"  # transfer ownership / next step
    fyi = "fyi"  # queue until next session
    answer = "answer"  # reply to a question
    artifact = "artifact"  # substantive attachment pointer / inline payload


# Single source of truth for "does this Mess spawn a tool session" — used by
# Mess.wakes_agent(), the daemon poll loop, and the store's circuit breaker.
WAKE_KINDS: tuple[MessKind, ...] = (MessKind.question, MessKind.handoff, MessKind.answer)

DEFAULT_CIRCUIT: dict[str, int] = {"max_spawns": 20, "window_s": 300, "max_hop_depth": 10}


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
    repos: list[str] = Field(default_factory=list)
    paused_reason: str | None = None
    circuit: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_CIRCUIT))
    label: str | None = None

    @classmethod
    def create(
        cls,
        name: str,
        agents: list[str],
        meta: dict[str, Any] | None = None,
        password: str | None = None,
        repos: list[str] | None = None,
        circuit: dict[str, int] | None = None,
    ) -> Jar:
        from messjar.auth import generate_password
        from messjar.repo import normalize_repo_key

        agents = list(dict.fromkeys(a.strip() for a in (agents or []) if a.strip()))
        # Agents join via the share link — creator does not pre-assign tools.
        repo_keys = []
        for r in repos or []:
            k = normalize_repo_key(r)
            if k and k not in repo_keys:
                repo_keys.append(k)
        # default: jar name is also a repo key so "ABC" jar matches project ABC
        name_key = normalize_repo_key(name)
        if name_key and name_key not in repo_keys:
            repo_keys.insert(0, name_key)
        now = _now()
        jar_id = f"jar_{uuid4().hex[:12]}"
        local = {a: AgentLocalState(agent_id=a) for a in agents}
        merged_circuit = dict(DEFAULT_CIRCUIT)
        merged_circuit.update({k: v for k, v in (circuit or {}).items() if k in DEFAULT_CIRCUIT})
        return cls(
            id=jar_id,
            name=name,
            agents=agents,
            local=local,
            created_at=now,
            meta=meta or {},
            password=(password.strip() if password and password.strip() else generate_password()),
            repos=repo_keys,
            circuit=merged_circuit,
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
    trigger_source: Literal["human", "agent"] = "human"
    # Non-null only on the message that opened a loop (loop_id == own id).
    # A closing reply is recognized via a `mess:<loop_id>` ref, not a second
    # column here — see Store.send().
    loop_id: str | None = None
    loop_state: Literal["open", "answered", "stale"] | None = None

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
        trigger_source: str = "human",
    ) -> Mess:
        if isinstance(kind, str):
            kind = MessKind(kind)
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > MAX_BODY_BYTES:
            raise ValueError(f"body exceeds {MAX_BODY_BYTES} bytes")
        refs = refs or []
        if kind in (MessKind.answer, MessKind.artifact) and not has_verifiable_ref(refs):
            raise ValueError(
                f"kind={kind.value} requires a verifiable ref "
                f"(sha:<commit>, file:<path>, or test:<summary>) in refs"
            )
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
            refs=refs,
            ts=_now(),
            schema_version=SCHEMA_VERSION,
            trigger_source=trigger_source,  # type: ignore[arg-type]
        )

    def wakes_agent(self) -> bool:
        """Daemon: should this Mess spawn a tool session now?"""
        return self.kind in WAKE_KINDS


class LabelProposal(BaseModel):
    """A pending change to a jar's label — a proposal, not a mutation.

    Applies to `jars.label` only once every current participant has
    accepted this exact `patch`. `accepted_by` / `diff` are not stored on
    this model — they're computed from the `approvals` table and from
    `label_diff(base_label, patch)` by the layer serializing this for
    display (Store stays focused on persistence).
    """

    id: str
    jar_id: str
    proposed_by: str
    base_label: str | None = None
    patch: str
    origin_mess_id: str | None = None
    status: Literal["pending", "applied", "rejected"] = "pending"
    created_at: str
    decided_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        jar_id: str,
        proposed_by: str,
        patch: str,
        base_label: str | None = None,
        origin_mess_id: str | None = None,
    ) -> LabelProposal:
        return cls(
            id=f"prop_{uuid4().hex[:12]}",
            jar_id=jar_id,
            proposed_by=proposed_by,
            base_label=base_label,
            patch=patch,
            origin_mess_id=origin_mess_id,
            created_at=_now(),
        )


def label_diff(old: str | None, new: str) -> str:
    """Display-only unified diff between the current and proposed label."""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="current label", tofile="proposed label")
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
