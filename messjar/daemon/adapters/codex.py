"""Codex adapter — OpenAI Codex CLI (`codex`) when available."""

from __future__ import annotations

import subprocess
from typing import Any

from messjar.daemon.adapters.base import Adapter, InvokeResult, build_prompt


class CodexAdapter(Adapter):
    name = "codex"

    def available(self) -> bool:
        return self.binary(["codex"]) is not None

    def invoke(
        self,
        mess: dict[str, Any],
        *,
        workdir: str,
        session_id: str | None,
        dry_run: bool = False,
        readonly: bool = False,
        label: str | None = None,
    ) -> InvokeResult:
        binary = self.binary(["codex"])
        prompt = build_prompt(mess, label=label)
        # `codex exec` is the non-interactive path in recent CLIs
        mode_flags = ["--sandbox", "read-only"] if readonly else ["--full-auto"]
        cmd = [binary or "codex", "exec", *mode_flags, prompt]
        if session_id:
            cmd.extend(["--session", session_id])

        if dry_run or not binary:
            return InvokeResult(
                ok=True,
                session_id=session_id,
                output="dry-run: would invoke codex",
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
            return InvokeResult(ok=False, error="codex timed out", command=cmd)
        except FileNotFoundError:
            return InvokeResult(ok=False, error="codex binary not found", command=cmd)
