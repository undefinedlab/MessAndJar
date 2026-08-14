"""Pluggable autonomous triggers — mirrors messjar.daemon.adapters' shape.

Every trigger here ends in the same place a human always could: a Mess
sent with trigger_source="agent", so it's subject to exactly the same
policy gate (Task 4) as anything else an agent originates — a triggered
handoff is Held for the sending human, and a triggered spawn on the
receiving end runs read-only (Task 1). Triggers don't get a shortcut
around any of that; they're just another way a Mess gets born.
"""
