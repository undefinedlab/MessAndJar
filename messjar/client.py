"""HTTP client for the Mess&Jar bus (CLI + daemon)."""

from __future__ import annotations

import os
from typing import Any

import httpx


class BusClient:
    def __init__(
        self,
        base_url: str,
        password: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password if password is not None else os.environ.get(
            "MESSJAR_PASSWORD"
        ) or os.environ.get("MESSJAR_TOKEN")
        headers: dict[str, str] = {}
        if self.password:
            headers["Authorization"] = f"Bearer {self.password}"
            headers["X-MessJar-Password"] = self.password
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BusClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    def create_jar(
        self, name: str, agents: list[str], meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = self._client.post("/jars", json={"name": name, "agents": agents, "meta": meta or {}})
        r.raise_for_status()
        return r.json()

    def list_jars(self, agent: str | None = None) -> list[dict[str, Any]]:
        params = {}
        if agent:
            params["agent"] = agent
        r = self._client.get("/jars", params=params)
        r.raise_for_status()
        return r.json()

    def get_jar(self, jar: str) -> dict[str, Any]:
        r = self._client.get(f"/jars/{jar}")
        r.raise_for_status()
        return r.json()

    def pause(self, jar: str) -> dict[str, Any]:
        r = self._client.post(f"/jars/{jar}/pause")
        r.raise_for_status()
        return r.json()

    def resume(self, jar: str) -> dict[str, Any]:
        r = self._client.post(f"/jars/{jar}/resume")
        r.raise_for_status()
        return r.json()

    def messes(self, jar: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        r = self._client.get(
            f"/jars/{jar}/messes", params={"after_seq": after_seq, "limit": limit}
        )
        r.raise_for_status()
        return r.json()

    def send(
        self,
        jar: str,
        *,
        from_agent: str,
        to_agent: str,
        body: str,
        kind: str,
        reply_expected: bool | None = None,
        hop: int = 0,
        refs: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jar": jar,
            "from": from_agent,
            "to": to_agent,
            "body": body,
            "kind": kind,
            "hop": hop,
            "refs": refs or [],
        }
        if reply_expected is not None:
            payload["reply_expected"] = reply_expected
        r = self._client.post("/send", json=payload)
        r.raise_for_status()
        return r.json()

    def mcp_call(self, name: str, arguments: dict[str, Any]) -> Any:
        r = self._client.post("/rpc/call", json={"name": name, "arguments": arguments})
        r.raise_for_status()
        data = r.json()
        content = data.get("content") or []
        if content and "json" in content[0]:
            return content[0]["json"]
        return data

    def check_jar(
        self,
        agent: str,
        jar: str | None = None,
        *,
        after_seq: int | None = None,
        ack: bool = True,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"agent": agent, "ack": ack}
        if jar:
            args["jar"] = jar
        if after_seq is not None:
            args["after_seq"] = after_seq
        return self.mcp_call("check_jar", args)

    def wait(
        self,
        agent: str,
        jar: str | None = None,
        *,
        timeout_s: float = 30.0,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"agent": agent, "timeout_s": timeout_s}
        if jar:
            args["jar"] = jar
        if after_seq is not None:
            args["after_seq"] = after_seq
        return self.mcp_call("wait", args)

    def update_local(
        self,
        jar: str,
        agent: str,
        *,
        workdir: str | None = None,
        session_id: str | None = None,
        hop: int | None = None,
        paused: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jar": jar, "agent": agent}
        if workdir is not None:
            payload["workdir"] = workdir
        if session_id is not None:
            payload["session_id"] = session_id
        if hop is not None:
            payload["hop"] = hop
        if paused is not None:
            payload["paused"] = paused
        r = self._client.post("/local", json=payload)
        r.raise_for_status()
        return r.json()
