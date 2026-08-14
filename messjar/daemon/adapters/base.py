"""Adapter interface: Mess → headless tool invocation + session tracking."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from messjar.daemon.triggers.on_blocked import BLOCKED_GUIDANCE


@dataclass
class InvokeResult:
    ok: bool
    session_id: str | None = None
    output: str = ""
    reply_body: str | None = None
    reply_kind: str | None = None
    refs: list[str] = field(default_factory=list)
    dry_run: bool = False
    command: list[str] = field(default_factory=list)
    error: str | None = None


class Adapter(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool:
        """True if the underlying CLI/binary is on PATH."""

    @abstractmethod
    def invoke(
        self,
        mess: dict[str, Any],
        *,
        workdir: str,
        session_id: str | None,
        dry_run: bool = False,
        readonly: bool = False,
        label: str | None = None,
        unverified_refs: list[str] | None = None,
    ) -> InvokeResult:
        """Turn a Mess into a tool run. May resume session_id when supported.

        readonly=True means this spawn was caused by another agent's message
        (trigger_source="agent"), not a human — the invocation must not be
        able to write to the filesystem.

        label is the jar's current agreed context (see schema.LabelProposal)
        — injected above the message body so a decision made days ago
        survives without replaying history.

        unverified_refs lists any refs on this Mess that claimed to be
        checkable (sha:/file:) but did not resolve in this workdir — surfaced
        loudly in the prompt rather than silently trusted.
        """

    def binary(self, candidates: list[str]) -> str | None:
        for c in candidates:
            path = shutil.which(c)
            if path:
                return path
        return None


def build_prompt(
    mess: dict[str, Any], *, label: str | None = None, unverified_refs: list[str] | None = None
) -> str:
    """Shared prompt wrapper so adapters stay thin."""
    label_block = ""
    if label:
        label_block = (
            f"--- jar label (persistent, agreed context — read this first) ---\n"
            f"{label}\n"
            f"--- end label ---\n\n"
        )
    warning_block = ""
    if unverified_refs:
        refs_list = ", ".join(unverified_refs)
        warning_block = (
            f"--- ⚠ UNVERIFIED REFS ---\n"
            f"This message claims evidence that did NOT resolve in this workdir: {refs_list}\n"
            f"Treat the claim below as unconfirmed until you check it yourself.\n"
            f"--- end warning ---\n\n"
        )
    kind = mess.get("kind", "fyi")
    refs = mess.get("refs") or []
    refs_line = ", ".join(refs) if refs else "(none)"
    return (
        label_block
        + warning_block
        + f"You are participating in a Mess&Jar peer conversation.\n"
        f"You are agent `{mess.get('to')}` receiving a `{kind}` from `{mess.get('from')}`.\n"
        f"Jar: {mess.get('jar_id')}  Mess: {mess.get('id')}  Hop: {mess.get('hop', 0)}\n"
        f"Refs: {refs_line}\n"
        f"Reply-expected: {mess.get('reply_expected', False)}\n\n"
        f"--- message body ---\n"
        f"{mess.get('body', '')}\n"
        f"--- end ---\n\n"
        f"Act on this in the current working directory. If a reply is expected, "
        f"finish with a short concrete answer suitable to send back as a Mess. "
        + BLOCKED_GUIDANCE
    )
