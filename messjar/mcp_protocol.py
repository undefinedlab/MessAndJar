"""Real MCP (Streamable HTTP) for Cursor / Claude — tools backed by the bus Store."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastmcp import FastMCP

from messjar.auth import AuthContext
from messjar.schema import Mess, MessKind
from messjar.store import Store

_auth: ContextVar[AuthContext | None] = ContextVar("messjar_mcp_auth", default=None)
_store: ContextVar[Store | None] = ContextVar("messjar_mcp_store", default=None)


def set_mcp_auth(auth: AuthContext | None) -> None:
    _auth.set(auth)


def set_mcp_store(store: Store) -> None:
    _store.set(store)


def get_auth() -> AuthContext:
    auth = _auth.get()
    if auth is None:
        raise PermissionError("unauthorized: missing agent key or jar password")
    return auth


def get_store() -> Store:
    store = _store.get()
    if store is None:
        raise RuntimeError("store not initialized")
    return store


def _require_jar(auth: AuthContext, jar) -> None:
    if not auth.allows_jar(jar.id, jar.name, jar.agents):
        raise PermissionError("forbidden: not allowed on this jar")


def _pick_jar(
    auth: AuthContext,
    *,
    agent: str | None = None,
    jar: str | None = None,
    repo: str | None = None,
    workdir: str | None = None,
):
    store = get_store()
    agent_id = agent or auth.agent_id
    if auth.jar_name and not jar and not repo and not workdir:
        found = store.get_jar(auth.jar_id or auth.jar_name)
        if found:
            _require_jar(auth, found)
            return found
    found = store.resolve_jar(
        agent_id=agent_id,
        jar=jar or auth.jar_name,
        repo=repo,
        workdir=workdir,
    )
    _require_jar(auth, found)
    return found


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name="messjar",
        instructions=(
            "Mess&Jar peer agent bus. One connection covers all jars you belong to. "
            "Always pass workdir (absolute project path) or repo (e.g. github.com/org/name) "
            "so the bus can pick the right jar. Use your agent id (e.g. zen@cursor) as "
            "`agent` / `from`. kind=question wakes the other side; kind=fyi is notify-only."
        ),
    )

    @mcp.tool
    def which_jar(
        agent: str,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
    ) -> dict[str, Any]:
        """Resolve which jar matches this agent + workdir/repo."""
        auth = get_auth()
        j = _pick_jar(auth, agent=agent, jar=jar, repo=repo, workdir=workdir)
        return {
            "jar": j.name,
            "id": j.id,
            "repos": j.repos,
            "agents": j.agents,
        }

    @mcp.tool
    def list_jars(agent: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        """List jars this agent is attached to."""
        auth = get_auth()
        store = get_store()
        if auth.admin or auth.open_bus:
            jars = store.list_jars(agent or auth.agent_id, include_archived)
        elif auth.agent_id:
            jars = store.list_jars(auth.agent_id, include_archived)
        else:
            j = store.get_jar(auth.jar_id or "")
            jars = [j] if j else []
        return [j.public_dict() for j in jars if j]

    @mcp.tool
    def send(
        body: str,
        kind: str,
        from_agent: str,
        to_agent: str,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
        reply_expected: bool | None = None,
        hop: int = 0,
        refs: list[str] | None = None,
        trigger_source: str = "human",
    ) -> dict[str, Any]:
        """Post a Mess. Pass workdir/repo (or jar) so the bus picks the thread.

        from_agent / to_agent are ids like zen@cursor.
        """
        auth = get_auth()
        store = get_store()
        j = _pick_jar(
            auth, agent=from_agent, jar=jar, repo=repo, workdir=workdir
        )
        mess = Mess.create(
            jar_id=j.id,
            from_agent=from_agent,
            to_agent=to_agent,
            body=body,
            kind=MessKind(kind),
            reply_expected=reply_expected,
            hop=hop,
            refs=refs or [],
            trigger_source=trigger_source,
        )
        return store.send(mess).model_dump(by_alias=True)

    @mcp.tool
    def update_label(
        patch: str,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
        agent: str | None = None,
        origin_mess_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose a change to the jar's label (persistent shared context).

        Does NOT write immediately — creates a pending proposal. It only
        takes effect once every participant on the jar accepts it, via the
        web UI or `mj label accept` (there is deliberately no MCP tool for
        accept/reject/edit — that's the human-confirmation checkpoint).
        """
        auth = get_auth()
        store = get_store()
        j = _pick_jar(auth, agent=agent, jar=jar, repo=repo, workdir=workdir)
        proposed_by = agent or auth.agent_id
        if not proposed_by:
            raise PermissionError("update_label requires an agent id")
        proposal = store.propose_label(
            j.id, proposed_by=proposed_by, patch=patch, origin_mess_id=origin_mess_id
        )
        return proposal.model_dump()

    @mcp.tool
    def check_jar(
        agent: str,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
        after_seq: int | None = None,
        limit: int = 50,
        ack: bool = True,
    ) -> list[dict[str, Any]]:
        """Non-blocking: new messes for this agent (scoped by jar or workdir/repo)."""
        auth = get_auth()
        store = get_store()
        jar_filter = jar
        if repo or workdir or (not jar_filter and auth.jar_name):
            j = _pick_jar(
                auth, agent=agent, jar=jar, repo=repo, workdir=workdir
            )
            jar_filter = j.name
        elif auth.jar_name and not jar_filter:
            jar_filter = auth.jar_name
        batch = store.check_jar(agent, jar_filter, after_seq=after_seq, limit=limit)
        if ack:
            for item in batch:
                found = store.get_jar(item["jar"]["id"])
                if found:
                    _require_jar(auth, found)
                store.advance_cursor(item["jar"]["id"], agent, item["cursor"])
        return batch

    @mcp.tool
    def wait(
        agent: str,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
        after_seq: int | None = None,
        timeout_s: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Block until a Mess arrives (or timeout)."""
        auth = get_auth()
        store = get_store()
        jar_filter = jar
        if repo or workdir or auth.jar_name:
            j = _pick_jar(
                auth, agent=agent, jar=jar, repo=repo, workdir=workdir
            )
            jar_filter = j.name
        batch = store.wait(
            agent,
            jar_id_or_name=jar_filter,
            after_seq=after_seq,
            timeout_s=timeout_s,
        )
        for item in batch:
            found = store.get_jar(item["jar"]["id"])
            if found:
                _require_jar(auth, found)
            store.advance_cursor(item["jar"]["id"], agent, item["cursor"])
        return batch

    return mcp
