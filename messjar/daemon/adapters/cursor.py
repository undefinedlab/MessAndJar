"""Cursor adapter — headless agent via `cursor` / `agent` CLI when available."""

from __future__ import annotations

import subprocess
from typing import Any

from messjar.daemon.adapters.base import Adapter, InvokeResult, build_prompt


class CursorAdapter(Adapter):
    name = "cursor"

    def available(self) -> bool:
        return self.binary(["cursor-agent", "agent", "cursor"]) is not None

    def invoke(
        self,
        mess: dict[str, Any],
        *,
        workdir: str,
        session_id: str | None,
        dry_run: bool = False,
        readonly: bool = False,
    ) -> InvokeResult:
        binary = self.binary(["cursor-agent", "agent", "cursor"])
        prompt = build_prompt(mess)
        # Prefer print/headless style; fall back to generic argv
        if binary and binary.endswith("cursor") and "agent" not in binary:
            cmd = [binary, "agent", "-p", prompt]
        else:
            cmd = [binary or "cursor-agent", "-p", prompt]
        if readonly:
            cmd.extend(["--mode", "plan"])
        if session_id:
            cmd.extend(["--resume", session_id])

        if dry_run or not binary:
            return InvokeResult(
                ok=True,
                session_id=session_id,
                output="dry-run: would invoke cursor agent",
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
            return InvokeResult(ok=False, error="cursor agent timed out", command=cmd)
        except FileNotFoundError:
            return InvokeResult(ok=False, error="cursor agent binary not found", command=cmd)
