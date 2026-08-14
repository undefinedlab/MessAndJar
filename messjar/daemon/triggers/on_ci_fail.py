"""on_ci_fail — CI job step: a question routed to the owning agent via
`## Owned by`.

Shipped as a CLI entry point (`mj trigger on-ci-fail`) rather than a new
HTTP webhook endpoint on the bus — a CI job step already runs arbitrary
shell commands, so this reuses the exact same agent-key auth the daemon
already has instead of designing a separate signing/secret scheme for
external callers hitting the bus directly. See `mj trigger on-ci-fail
--help` for the actual entry point; this module is the pure routing logic.
"""

from __future__ import annotations

from typing import Any

from messjar.policy import normalize_scope_path, parse_owned_by


def plan_ci_fail_notice(
    *,
    agents: list[str],
    from_agent: str,
    label: str | None,
    failing_paths: list[str],
    summary: str,
    sha: str | None = None,
    fallback_to: str | None = None,
) -> list[dict[str, Any]]:
    """A declared-owner match on any failing path -> targeted question to
    the owner(s). No match -> the caller's explicit --to fallback if given,
    else nothing: unlike on_session_end, a CI failure with no declared
    owner and no operator-chosen fallback has no default recipient and
    shouldn't spam the whole jar guessing at one.
    """
    owned_by = parse_owned_by(label)
    normalized_paths = [normalize_scope_path(p) for p in failing_paths]
    owners: set[str] = set()
    for path in normalized_paths:
        for agent, prefixes in owned_by.items():
            if agent == from_agent or agent not in agents:
                continue
            if any(path.startswith(prefix) for prefix in prefixes):
                owners.add(agent)
    if not owners and fallback_to and fallback_to != from_agent and fallback_to in agents:
        owners = {fallback_to}
    if not owners:
        return []
    refs = [f"path:{p}" for p in normalized_paths]
    if sha:
        refs.append(f"sha:{sha}")
    return [
        {
            "to_agent": a,
            "kind": "question",
            "reply_expected": True,
            "body": f"CI failed — {summary}",
            "refs": refs,
        }
        for a in sorted(owners)
    ]
