"""on_blocked — not invoked; a permanent addition to every spawn's prompt.

Mostly a system-prompt change, per the brief, and the trigger that delivers
the most value for the least code: if an agent is blocked on something
outside its own declared scope, it should ask rather than guess, stub, or
work around it silently. `daemon/adapters/base.py::build_prompt()` appends
BLOCKED_GUIDANCE to its closing instruction on every single prompt it
builds — unconditional, not gated behind a flag, since "ask before you
guess" is cheap, always-relevant advice whether or not this particular
jar has declared any `## Owned by` scope yet.
"""

from __future__ import annotations

BLOCKED_GUIDANCE = (
    "If you get blocked on something outside your own declared scope "
    "(see '## Owned by' above, if declared), do not guess, stub, or work "
    "around it silently — send a `question` Mess to the agent who owns "
    "that area and wait for their answer."
)
