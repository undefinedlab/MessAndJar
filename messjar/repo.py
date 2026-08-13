"""Map workdirs / git remotes → jar repo keys."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse


def normalize_repo_key(raw: str) -> str:
    """Normalize a user-entered or discovered repo identifier."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.rstrip("/")
    # file path → basename
    if s.startswith("/") or s.startswith("~") or s.startswith("."):
        return Path(s).expanduser().resolve().name.lower()
    # git@host:org/repo.git
    m = re.match(r"^git@([^:]+):(.+)$", s)
    if m:
        host, path = m.group(1).lower(), m.group(2)
        path = re.sub(r"\.git$", "", path)
        return f"{host}/{path}".lower()
    # https://github.com/org/repo.git
    if "://" in s:
        p = urlparse(s)
        host = (p.hostname or "").lower()
        path = re.sub(r"\.git$", "", (p.path or "").strip("/"))
        return f"{host}/{path}".lower() if host and path else s.lower()
    # already org/repo or bare name
    s = re.sub(r"\.git$", "", s)
    return s.lower()


def keys_from_workdir(workdir: str | None) -> list[str]:
    if not workdir:
        return []
    path = Path(workdir).expanduser()
    keys = [normalize_repo_key(str(path))]
    # try git remote
    git = path / ".git"
    if git.exists():
        config = path / ".git" / "config"
        if config.is_file():
            text = config.read_text(errors="ignore")
            for m in re.finditer(r'url\s*=\s*(.+)', text):
                keys.append(normalize_repo_key(m.group(1).strip()))
    # unique preserve order
    out: list[str] = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def candidate_keys(*, jar: str | None = None, repo: str | None = None, workdir: str | None = None) -> list[str]:
    keys: list[str] = []
    if jar:
        keys.append(normalize_repo_key(jar))
    if repo:
        keys.append(normalize_repo_key(repo))
    keys.extend(keys_from_workdir(workdir))
    out: list[str] = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out
