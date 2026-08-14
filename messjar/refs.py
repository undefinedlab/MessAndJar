"""Verifiable refs for kind=answer/artifact — pure logic, no DB.

A ref is a plain string. Three prefixes carry an evidentiary claim:
  sha:<hex>    — a commit that must exist in the receiver's local git repo
  file:<path>  — a file that must exist in the receiver's workdir
  test:<text>  — free-form test output; not independently checkable, presence-only

Anything else (including the `mess:<id>` refs used to thread replies) is
just context, not evidence — it doesn't count toward the "at least one
verifiable ref" requirement and isn't something `verify_ref` can resolve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REQUIRED_REF_PREFIXES = ("sha:", "file:", "test:")


def has_verifiable_ref(refs: list[str]) -> bool:
    return any(r.startswith(REQUIRED_REF_PREFIXES) for r in refs)


def verify_ref(ref: str, workdir: str) -> bool | None:
    """True/False if this ref's claim can be checked and was/wasn't found;
    None if this ref type isn't independently resolvable (not a failure)."""
    if ref.startswith("sha:"):
        sha = ref[len("sha:") :].strip()
        if not sha:
            return False
        try:
            proc = subprocess.run(
                ["git", "cat-file", "-e", sha],
                cwd=workdir,
                capture_output=True,
                timeout=5,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False  # no git, not a repo, timeout — fail closed
    if ref.startswith("file:"):
        path = ref[len("file:") :].strip()
        if not path:
            return False
        return (Path(workdir) / path).exists()
    return None
