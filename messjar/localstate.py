"""Local kill switch — pure filesystem, no network/DB.

`mj pause --all` must work even when the bus is unreachable, so this module
never imports Store/BusClient. A daemon checks `killswitch_state()` before
every poll tick, before it makes any network call at all.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("MESSJAR_STATE_DIR") or (Path.home() / ".messjar"))
KILLSWITCH_FILE = STATE_DIR / "killswitch.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def engage_killswitch(reason: str | None = None) -> dict[str, Any]:
    """Write the local kill-switch marker file. No network/DB."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {"paused_at": _now_iso(), "reason": reason}
    KILLSWITCH_FILE.write_text(json.dumps(state))
    return state


def release_killswitch() -> bool:
    """Remove the marker file if present. Returns True if it existed."""
    if KILLSWITCH_FILE.exists():
        KILLSWITCH_FILE.unlink()
        return True
    return False


def killswitch_state() -> dict[str, Any] | None:
    """None if not engaged; a dict (paused_at, reason) if engaged.

    Fail closed: an unreadable/corrupt file is treated as engaged rather
    than silently ignored.
    """
    if not KILLSWITCH_FILE.exists():
        return None
    try:
        return json.loads(KILLSWITCH_FILE.read_text())
    except Exception:
        return {"paused_at": None, "reason": "unreadable killswitch file"}
