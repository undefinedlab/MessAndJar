"""Desktop notifications when a Mess arrives.

Cursor/Claude Code only notify for *their own* agent turns — there is no
public inbox API for third-party buses. OS notifications are the portable
way to tap you when a colleague messes your jar.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any

log = logging.getLogger("messjar.notify")


def notify(title: str, body: str, *, urgency: str = "normal") -> bool:
    """Best-effort desktop notification. Returns True if a notifier ran."""
    title = (title or "Mess&Jar")[:80]
    body = (body or "")[:280]
    try:
        if sys.platform == "darwin":
            return _macos(title, body)
        if sys.platform == "win32":
            return _windows(title, body)
        return _linux(title, body, urgency=urgency)
    except Exception:
        log.exception("notify failed")
        return False


def notify_mess(jar: dict[str, Any], mess: dict[str, Any]) -> bool:
    kind = mess.get("kind") or "mess"
    jar_name = jar.get("name") or jar.get("id") or "jar"
    sender = mess.get("from") or "?"
    preview = (mess.get("body") or "").strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "…"
    title = f"Mess&Jar · {jar_name}"
    body = f"{kind} from {sender}: {preview}" if preview else f"{kind} from {sender}"
    urgency = "critical" if kind in ("question", "handoff") else "normal"
    return notify(title, body, urgency=urgency)


def notify_circuit_trip(jar: dict[str, Any]) -> bool:
    jar_name = jar.get("name") or jar.get("id") or "jar"
    reason = jar.get("paused_reason") or "circuit breaker tripped"
    title = f"Mess&Jar · {jar_name}"
    body = f"circuit breaker tripped: {reason}"
    return notify(title, body, urgency="critical")


def _linux(title: str, body: str, *, urgency: str) -> bool:
    if shutil.which("notify-send"):
        subprocess.run(
            [
                "notify-send",
                "--app-name=Mess&Jar",
                f"--urgency={urgency}",
                title,
                body,
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return True
    log.debug("notify-send not available")
    return False


def _macos(title: str, body: str) -> bool:
    # Escape for AppleScript string
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=5)
    return True


def _windows(title: str, body: str) -> bool:
    # PowerShell balloon via BurntToast is optional; fall back to msg-less log
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        # Minimal fallback: write-host won't show as toast without WinRT template
        f"Write-Output '{title}: {body}'"
    )
    # Prefer powershell toast if available via a simple .NET balloon
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(5000, '{title.replace("'", "''")}', '{body.replace("'", "''")}', [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 6
$n.Dispose()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        timeout=15,
    )
    return True
