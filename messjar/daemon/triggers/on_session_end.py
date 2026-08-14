"""on_session_end — Claude Code Stop hook: summarize changes, flag what is
now someone else's problem.

This is the trigger that delivers the README's actual promise: your agent
finishes, notices the next step isn't yours, and hands it off before you
close the laptop. Two parts, deliberately separated:

- git_session_changes(): I/O, best-effort, never raises — reads real git
  state so the notice is grounded in something verifiable rather than a
  model's own account of what it did.
- plan_session_end_notice(): pure. Matches the changed paths against the
  jar's declared `## Owned by` scope (reusing messjar.policy.parse_owned_by,
  not reimplementing it) to decide who gets a targeted handoff vs. a
  broadcast fyi.
"""

from __future__ import annotations

import subprocess
from typing import Any

from messjar.policy import normalize_scope_path, parse_owned_by


def git_session_changes(workdir: str) -> tuple[str, list[str]]:
    """Best-effort summary of what changed in workdir. Returns ("", []) if
    git isn't available, this isn't a repo, or nothing changed — never
    raises, since a broken git call shouldn't crash a hook.
    """
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return "", []
    if status.returncode != 0:
        return "", []
    paths: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # renames: "XY old -> new"
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.append(path)
    if not paths:
        return "", []
    preview = ", ".join(paths[:5]) + ("…" if len(paths) > 5 else "")
    return f"{len(paths)} file(s) changed: {preview}", paths


def plan_session_end_notice(
    *,
    agents: list[str],
    from_agent: str,
    label: str | None,
    changed_paths: list[str],
    summary: str,
) -> list[dict[str, Any]]:
    """Nothing changed -> nothing to say. Otherwise: a declared-owner match
    on any changed path becomes a targeted handoff to that owner (excluding
    a match on the sender's own declared paths — that's not someone else's
    problem); no match at all falls back to a broadcast fyi to every other
    agent on the jar, per the confirmed design decision.
    """
    if not changed_paths:
        return []
    owned_by = parse_owned_by(label)
    normalized_paths = [normalize_scope_path(p) for p in changed_paths]
    owners: set[str] = set()
    for path in normalized_paths:
        for agent, prefixes in owned_by.items():
            if agent == from_agent or agent not in agents:
                continue
            if any(path.startswith(prefix) for prefix in prefixes):
                owners.add(agent)
    if owners:
        refs = [f"path:{p}" for p in normalized_paths]
        return [
            {
                "to_agent": a,
                "kind": "handoff",
                "reply_expected": True,
                "body": f"session ended — {summary}",
                "refs": refs,
            }
            for a in sorted(owners)
        ]
    return [
        {"to_agent": a, "kind": "fyi", "body": f"session ended — {summary}", "refs": [], "reply_expected": False}
        for a in agents
        if a != from_agent
    ]
