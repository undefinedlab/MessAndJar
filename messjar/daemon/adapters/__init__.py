from messjar.daemon.adapters.base import Adapter, InvokeResult, build_prompt
from messjar.daemon.adapters.claude_code import ClaudeCodeAdapter
from messjar.daemon.adapters.codex import CodexAdapter
from messjar.daemon.adapters.cursor import CursorAdapter
from messjar.daemon.adapters.opencode import OpenCodeAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "cursor": CursorAdapter,
    "codex": CodexAdapter,
    "opencode": OpenCodeAdapter,
}


def get_adapter(name: str) -> Adapter:
    try:
        cls = ADAPTERS[name]
    except KeyError as e:
        known = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"unknown adapter {name!r}; known: {known}") from e
    return cls()


__all__ = [
    "ADAPTERS",
    "Adapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "OpenCodeAdapter",
    "InvokeResult",
    "build_prompt",
    "get_adapter",
]
