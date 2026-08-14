"""Claude Code adapter — headless `claude` CLI when available."""

from __future__ import annotations

import subprocess
from typing import Any

from messjar.daemon.adapters.base import Adapter, InvokeResult, build_prompt

# Read/search only, no shell or file writes — used for agent-triggered spawns.
READONLY_ALLOWED_TOOLS = "Read,Grep,Glob,WebFetch"


class ClaudeCodeAdapter(Adapter):
    name = "claude_code"

    def available(self) -> bool:
        return self.binary(["claude"]) is not None

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
        claude = self.binary(["claude"])
        prompt = build_prompt(mess, label=label, unverified_refs=unverified_refs)
        cmd = [claude or "claude", "-p", prompt, "--output-format", "text"]
        if readonly:
            cmd.extend(["--allowedTools", READONLY_ALLOWED_TOOLS])
        if session_id:
            cmd.extend(["--resume", session_id])

        if dry_run or not claude:
            return InvokeResult(
                ok=True,
                session_id=session_id,
                output="dry-run: would invoke claude",
                dry_run=True,
                command=cmd,
                reply_body=(
                    f"(dry-run/{self.name}) acknowledged {mess.get('kind')} "
                    f"from {mess.get('from')}: {str(mess.get('body', ''))[:200]}"
                ),
                reply_kind="answer" if mess.get("reply_expected") else None,
            )

        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                return InvokeResult(
                    ok=False,
                    session_id=session_id,
                    output=out,
                    error=err or f"exit {proc.returncode}",
                    command=cmd,
                )
            reply_kind = "answer" if mess.get("reply_expected") else None
            return InvokeResult(
                ok=True,
                session_id=session_id,
                output=out,
                reply_body=out if reply_kind else None,
                reply_kind=reply_kind,
                command=cmd,
            )
        except subprocess.TimeoutExpired:
            return InvokeResult(ok=False, error="claude timed out", command=cmd)
        except FileNotFoundError:
            return InvokeResult(ok=False, error="claude binary not found", command=cmd)
