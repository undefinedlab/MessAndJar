"""Adapter interface: Mess → headless tool invocation + session tracking."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
    ) -> InvokeResult:
        """Turn a Mess into a tool run. May resume session_id when supported."""

    def binary(self, candidates: list[str]) -> str | None:
        for c in candidates:
            path = shutil.which(c)
            if path:
                return path
        return None


def build_prompt(mess: dict[str, Any]) -> str:
    """Shared prompt wrapper so adapters stay thin."""
    kind = mess.get("kind", "fyi")
    refs = mess.get("refs") or []
    refs_line = ", ".join(refs) if refs else "(none)"
    return (
        f"You are participating in a Mess&Jar peer conversation.\n"
        f"You are agent `{mess.get('to')}` receiving a `{kind}` from `{mess.get('from')}`.\n"
        f"Jar: {mess.get('jar_id')}  Mess: {mess.get('id')}  Hop: {mess.get('hop', 0)}\n"
        f"Refs: {refs_line}\n"
        f"Reply-expected: {mess.get('reply_expected', False)}\n\n"
        f"--- message body ---\n"
        f"{mess.get('body', '')}\n"
        f"--- end ---\n\n"
        f"Act on this in the current working directory. If a reply is expected, "
        f"finish with a short concrete answer suitable to send back as a Mess."
    )
