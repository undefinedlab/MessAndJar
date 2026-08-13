"""Share-link connection snippets for Cursor / Claude Code / Codex / OpenCode."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote


def public_base(request_base: str) -> str:
    return request_base.rstrip("/")


def share_path(jar_name: str, password: str) -> str:
    return f"/j/{quote(jar_name)}?p={quote(password)}"


def agent_mcp_bundle(*, base_url: str, agent_id: str, token: str) -> dict[str, Any]:
    base = public_base(base_url)
    mcp = {
        "mcpServers": {
            "messjar": {
                "url": f"{base}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return {
        "bus_url": base,
        "agent_id": agent_id,
        "token": token,
        "mcp_json": mcp,
        "mcp_json_text": json.dumps(mcp, indent=2),
        "hint": (
            "One MCP connection for all jars you're on. "
            "When calling tools, pass workdir (your repo path) or repo "
            "(github.com/org/name) — the bus picks the matching jar."
        ),
        "cli_export": (
            f"export MESSJAR_BUS={base}\n"
            f"export MESSJAR_PASSWORD='{token}'\n"
            f"# agent: {agent_id} — jar is chosen from workdir/repo"
        ),
        "daemon": {
            "cursor": (
                f"mj daemon run --agent {agent_id} --adapter cursor "
                f"--bus {base} --password '{token}' --workdir ."
            ),
            "claude": (
                f"mj daemon run --agent {agent_id} --adapter claude_code "
                f"--bus {base} --password '{token}' --workdir ."
            ),
            "claude_code": (
                f"mj daemon run --agent {agent_id} --adapter claude_code "
                f"--bus {base} --password '{token}' --workdir ."
            ),
            "codex": (
                f"mj daemon run --agent {agent_id} --adapter codex "
                f"--bus {base} --password '{token}' --workdir ."
            ),
            "opencode": (
                f"mj daemon run --agent {agent_id} --adapter opencode "
                f"--bus {base} --password '{token}' --workdir ."
            ),
        },
    }


def connection_bundle(
    *,
    base_url: str,
    jar_name: str,
    password: str,
    agents: list[str],
    repos: list[str] | None = None,
) -> dict[str, Any]:
    base = public_base(base_url)
    share_url = f"{base}{share_path(jar_name, password)}"
    mcp = {
        "mcpServers": {
            "messjar": {
                "url": f"{base}/mcp",
                "headers": {"Authorization": f"Bearer {password}"},
            }
        }
    }
    env = {
        "MESSJAR_BUS": base,
        "MESSJAR_PASSWORD": password,
    }
    daemons = {
        "cursor": (
            f"mj daemon run --agent YOUR_ID@cursor --adapter cursor "
            f"--jar {jar_name} --bus {base} --password '{password}' --workdir ."
        ),
        "claude_code": (
            f"mj daemon run --agent YOUR_ID@claude --adapter claude_code "
            f"--jar {jar_name} --bus {base} --password '{password}' --workdir ."
        ),
        "codex": (
            f"mj daemon run --agent YOUR_ID@codex --adapter codex "
            f"--jar {jar_name} --bus {base} --password '{password}' --workdir ."
        ),
        "opencode": (
            f"mj daemon run --agent YOUR_ID@opencode --adapter opencode "
            f"--jar {jar_name} --bus {base} --password '{password}' --workdir ."
        ),
    }
    return {
        "bus_url": base,
        "jar": jar_name,
        "password": password,
        "agents": agents,
        "repos": repos or [jar_name.lower()],
        "share_url": share_url,
        "mcp_json": mcp,
        "mcp_json_text": json.dumps(mcp, indent=2),
        "env": env,
        "daemon": daemons,
        "cli_export": (
            f"export MESSJAR_BUS={base}\n"
            f"export MESSJAR_PASSWORD='{password}'\n"
            f"# jar: {jar_name}\n"
            f"# repos: {', '.join(repos or [jar_name])}\n"
            f"# agents: {', '.join(agents)}"
        ),
    }
