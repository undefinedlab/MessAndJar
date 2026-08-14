"""on_push — git pre-push hook: notify the jar with an fyi + the pushed SHA.

Git has no native "post-push" hook; `pre-push` is the closest real one —
it runs as part of `git push`, after the commits already exist, which is
what this is meant to be installed as. See the CLI's `mj trigger on-push`
for the actual hook-facing entry point; this module is just the pure
planning logic.
"""

from __future__ import annotations

from typing import Any


def plan_push_notice(
    *, agents: list[str], from_agent: str, sha: str, branch: str | None = None
) -> list[dict[str, Any]]:
    """Fan an fyi out to every other agent on the jar. A push is visibility,
    not a claim needing evidence — fyi never gets held or scope-checked, so
    this always delivers immediately regardless of jar policy.
    """
    body = f"pushed {sha[:12]}" + (f" on {branch}" if branch else "")
    return [
        {"to_agent": a, "kind": "fyi", "body": body, "refs": [f"sha:{sha}"], "reply_expected": False}
        for a in agents
        if a != from_agent
    ]
