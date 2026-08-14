"""Policy gate: tiers messages by kind/trigger_source, enforces `## Owned by`
scope for handoffs, and routes Held-tier sends into an approval queue
instead of delivering immediately.

This is the single choke point between the transport layer
(bus/server.py, mcp_protocol.py) and Store.send() — those are the only
two places in the codebase that create a new Mess, and both call
`policy.send()` here instead of `store.send()` directly. Nothing about
this module can be trusted to run on the sending agent's side; every
check happens against data the bus already has (the jar's own stored
label, the message's own declared kind/trigger_source) so a fully
subverted sending agent's prompt cannot talk its way past it.
"""

from __future__ import annotations

from typing import Any

from messjar.schema import DEFAULT_POLICY, HeldMessage, Jar, Mess, MessKind
from messjar.store import Store

HANDOFF_PATH_PREFIX = "path:"


class ScopeViolation(ValueError):
    """A handoff's target path falls outside the recipient's declared scope."""


def normalize_scope_path(path: str) -> str:
    """Canonicalize a path for `## Owned by` comparison: always a leading
    slash. `## Owned by` entries are conventionally written /like/this, but
    real-world path sources disagree — git reports paths relative to the
    repo root with no leading slash, CI tooling varies by provider. Both
    sides of every scope comparison go through this so `/src/x` and `src/x`
    are treated as the same claim.
    """
    return "/" + path.strip().lstrip("/")


def parse_owned_by(label: str | None) -> dict[str, list[str]]:
    """Parse a `## Owned by` section (`- agent_id: /path` bullets) out of a
    jar's label into {agent_id: [path prefixes]}. Sections are convention,
    not schema (per Task 2) — this is a best-effort text parse, not a
    strict format; unparsable lines are just skipped.
    """
    if not label:
        return {}
    owned: dict[str, list[str]] = {}
    in_section = False
    for line in label.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = stripped.lstrip("#").strip().lower() == "owned by"
            continue
        if not in_section or not stripped.startswith("-"):
            continue
        body = stripped.lstrip("-").strip()
        if ":" not in body:
            continue
        agent, path = body.split(":", 1)
        agent = agent.strip()
        path = path.strip()
        if not agent or not path:
            continue
        owned.setdefault(agent, []).append(normalize_scope_path(path))
    return owned


def check_scope(jar: Jar, mess: Mess) -> None:
    """Raise ScopeViolation if a handoff's declared target path(s) fall
    outside the recipient's declared `## Owned by` scope. A no-op unless
    both a scope AND a path claim exist to check against each other —
    see design decision 1: enforcement only ever narrows a boundary the
    jar's label already stated, never invents one retroactively.

    Exempt for trigger_source="human", matching tier_for()'s own rule and
    Task 1's readonly-spawn precedent: the threat model here is a *sending
    agent's prompt* being subverted into crafting an out-of-scope handoff,
    not a human directly typing a command with full awareness of what
    they're doing. A human who disagrees with a declared scope should
    change the label, not be silently blocked by it.
    """
    if mess.trigger_source == "human":
        return
    if mess.kind != MessKind.handoff:
        return
    owned_by = parse_owned_by(jar.label)
    recipient_scope = owned_by.get(mess.to_agent)
    if not recipient_scope:
        return
    target_paths = [
        normalize_scope_path(r[len(HANDOFF_PATH_PREFIX) :])
        for r in mess.refs
        if r.startswith(HANDOFF_PATH_PREFIX)
    ]
    if not target_paths:
        return
    for path in target_paths:
        if not any(path.startswith(prefix) for prefix in recipient_scope):
            raise ScopeViolation(
                f"handoff path {path!r} is outside {mess.to_agent}'s declared scope "
                f"({', '.join(recipient_scope)})"
            )


def tier_for(jar: Jar, mess: Mess) -> str:
    """'free' sends immediately; 'held' queues for the sender's human.

    A human's own action is already the approval — trigger_source=human
    is always free regardless of kind. Only an agent-originated send of a
    configured held_kind (handoff/artifact by default) is held.
    """
    if mess.trigger_source == "human":
        return "free"
    held_kinds = set(jar.policy.get("held_kinds", DEFAULT_POLICY["held_kinds"]))
    return "held" if mess.kind.value in held_kinds else "free"


def send(store: Store, mess: Mess, *, client: str = "api") -> Mess | HeldMessage:
    jar = store.get_jar(mess.jar_id)
    if not jar:
        raise KeyError(f"jar not found: {mess.jar_id}")
    check_scope(jar, mess)
    if tier_for(jar, mess) == "held":
        held = store.hold_message(mess)
        store.write_audit(
            jar.id, actor=mess.from_agent, action="held", target_id=held.id,
            detail=f"kind={mess.kind.value}", client=client,
        )
        return held
    return store.send(mess)


def public_dict(item: Mess | HeldMessage) -> dict[str, Any]:
    """Uniform serialization regardless of which tier a send resolved to."""
    if isinstance(item, Mess):
        return item.model_dump(by_alias=True)
    return item.model_dump()
