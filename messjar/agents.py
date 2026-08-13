"""Agent id helpers — assigned on join, not guessed by the jar creator."""

from __future__ import annotations

import re
from uuid import uuid4

# UI tool → agent id suffix
TOOL_SUFFIX = {
    "cursor": "cursor",
    "claude": "claude",
    "claude_code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}

TOOL_LABELS = {
    "cursor": "Cursor",
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
}


def normalize_tool(tool: str) -> str:
    t = (tool or "").strip().lower().replace(" ", "_").replace("-", "_")
    if t not in TOOL_SUFFIX:
        raise ValueError(
            f"unknown tool {tool!r}; choose: {', '.join(sorted(TOOL_LABELS))}"
        )
    return t


def slug_handle(display_name: str | None) -> str:
    raw = (display_name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not slug:
        slug = f"agent-{uuid4().hex[:6]}"
    return slug[:32]


def make_agent_id(display_name: str | None, tool: str) -> str:
    tool = normalize_tool(tool)
    return f"{slug_handle(display_name)}@{TOOL_SUFFIX[tool]}"
