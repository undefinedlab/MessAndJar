"""Adapter command-building: read-only (agent-triggered) vs full-access spawns.

Pure unit tests — dry_run=True short-circuits every adapter before it ever
calls subprocess.run, so these run without any real CLI binary installed
and without a DATABASE_URL. This is the acceptance check for Task 1.1's
"no write access" requirement, since a live cursor-agent/claude/codex
install isn't available in CI.
"""

from __future__ import annotations

import pytest

from messjar.daemon.adapters import (
    ClaudeCodeAdapter,
    CodexAdapter,
    CursorAdapter,
    OpenCodeAdapter,
)
from messjar.daemon.adapters.claude_code import READONLY_ALLOWED_TOOLS

FAKE_MESS = {
    "id": "msg_1",
    "jar_id": "jar_1",
    "from": "bob@claude",
    "to": "alex@cursor",
    "body": "hello",
    "kind": "question",
    "reply_expected": True,
    "hop": 0,
    "refs": [],
}


def _invoke(adapter, *, readonly: bool):
    return adapter.invoke(FAKE_MESS, workdir=".", session_id=None, dry_run=True, readonly=readonly)


def test_cursor_readonly_uses_plan_mode() -> None:
    cmd = _invoke(CursorAdapter(), readonly=True).command
    assert "--mode" in cmd and "plan" in cmd


def test_cursor_full_access_has_no_mode_flag() -> None:
    cmd = _invoke(CursorAdapter(), readonly=False).command
    assert "--mode" not in cmd


def test_claude_code_readonly_restricts_tools() -> None:
    cmd = _invoke(ClaudeCodeAdapter(), readonly=True).command
    assert "--allowedTools" in cmd
    assert READONLY_ALLOWED_TOOLS in cmd


def test_claude_code_full_access_has_no_allowed_tools_flag() -> None:
    cmd = _invoke(ClaudeCodeAdapter(), readonly=False).command
    assert "--allowedTools" not in cmd


def test_codex_readonly_uses_sandbox_flag() -> None:
    cmd = _invoke(CodexAdapter(), readonly=True).command
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--full-auto" not in cmd


def test_codex_full_access_uses_full_auto() -> None:
    cmd = _invoke(CodexAdapter(), readonly=False).command
    assert "--full-auto" in cmd
    assert "--sandbox" not in cmd


def test_opencode_readonly_is_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        _invoke(OpenCodeAdapter(), readonly=True)


def test_opencode_full_access_unchanged() -> None:
    result = _invoke(OpenCodeAdapter(), readonly=False)
    assert "run" in result.command
    assert "--session" not in result.command  # no session_id passed in this test
