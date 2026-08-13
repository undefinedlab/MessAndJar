"""PostgreSQL persistence for Jars and Messes."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlparse

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from messjar.schema import AgentLocalState, Jar, Mess, MessKind


def normalize_database_url(url: str) -> str:
    """Railway sometimes gives postgres://; psycopg wants postgresql://."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


class Store:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        raw = database_url or os.environ.get("DATABASE_URL")
        if not raw:
            raise ValueError("DATABASE_URL is required")
        self.database_url = normalize_database_url(raw)
        self._pool = ConnectionPool(
            conninfo=self.database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._migrate()

    def close(self) -> None:
        self._pool.close()

    def _migrate(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jars (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    agents_json JSONB NOT NULL,
                    local_json JSONB NOT NULL,
                    paused BOOLEAN NOT NULL DEFAULT FALSE,
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL,
                    meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    password TEXT,
                    repos_json JSONB NOT NULL DEFAULT '[]'::jsonb
                );

                CREATE TABLE IF NOT EXISTS messes (
                    id TEXT PRIMARY KEY,
                    jar_id TEXT NOT NULL REFERENCES jars(id),
                    seq INTEGER NOT NULL,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reply_expected BOOLEAN NOT NULL,
                    hop INTEGER NOT NULL,
                    refs_json JSONB NOT NULL,
                    ts TIMESTAMPTZ NOT NULL,
                    schema_version INTEGER NOT NULL,
                    UNIQUE (jar_id, seq)
                );

                CREATE TABLE IF NOT EXISTS agent_keys (
                    agent_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_messes_jar_seq ON messes (jar_id, seq);
                CREATE INDEX IF NOT EXISTS idx_messes_to ON messes (to_agent, jar_id, seq);
                CREATE INDEX IF NOT EXISTS idx_agent_keys_token ON agent_keys (token);
                """
            )
            conn.execute(
                """
                ALTER TABLE jars ADD COLUMN IF NOT EXISTS password TEXT;
                ALTER TABLE jars ADD COLUMN IF NOT EXISTS repos_json JSONB NOT NULL DEFAULT '[]'::jsonb;
                CREATE INDEX IF NOT EXISTS idx_jars_password ON jars (password);
                """
            )
            conn.commit()

    def create_jar(
        self,
        name: str,
        agents: list[str],
        meta: dict[str, Any] | None = None,
        password: str | None = None,
        repos: list[str] | None = None,
    ) -> Jar:
        jar = Jar.create(name=name, agents=agents, meta=meta, password=password, repos=repos)
        with self._pool.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO jars (
                        id, name, agents_json, local_json, paused, archived,
                        created_at, meta_json, password, repos_json
                    )
                    VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
                    """,
                    (
                        jar.id,
                        jar.name,
                        json.dumps(jar.agents),
                        json.dumps({k: v.model_dump() for k, v in jar.local.items()}),
                        jar.paused,
                        jar.archived,
                        jar.created_at,
                        json.dumps(jar.meta),
                        jar.password,
                        json.dumps(jar.repos),
                    ),
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                if "unique" in str(e).lower():
                    raise ValueError(f"jar name already exists: {name}") from e
                raise
        return jar

    def get_jar_by_password(self, password: str) -> Jar | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jars WHERE password = %s AND archived = FALSE LIMIT 1",
                (password,),
            ).fetchone()
        return self._row_to_jar(row) if row else None

    def upsert_agent_key(self, agent_id: str, token: str | None = None) -> tuple[str, str]:
        from messjar.auth import generate_agent_key

        token = token or generate_agent_key()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT token FROM agent_keys WHERE agent_id = %s",
                (agent_id,),
            ).fetchone()
            if row:
                return agent_id, row["token"]
            conn.execute(
                "INSERT INTO agent_keys (agent_id, token) VALUES (%s, %s)",
                (agent_id, token),
            )
            conn.commit()
        return agent_id, token

    def get_agent_by_token(self, token: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT agent_id FROM agent_keys WHERE token = %s",
                (token,),
            ).fetchone()
        return row["agent_id"] if row else None

    def resolve_jar(
        self,
        *,
        agent_id: str | None,
        jar: str | None = None,
        repo: str | None = None,
        workdir: str | None = None,
    ) -> Jar:
        """Pick the jar for this agent from explicit name or repo/workdir bindings."""
        from messjar.repo import candidate_keys

        if jar:
            found = self.get_jar(jar)
            if not found:
                raise KeyError(f"jar not found: {jar}")
            if agent_id and agent_id not in found.agents:
                raise PermissionError(f"agent {agent_id} is not on jar {found.name}")
            return found

        keys = candidate_keys(repo=repo, workdir=workdir)
        if not keys:
            raise ValueError("provide jar, or repo/workdir so the bus can pick the jar")

        candidates = self.list_jars(agent_id=agent_id, include_archived=False)
        matches: list[Jar] = []
        for j in candidates:
            jar_keys = set(j.repos) | {j.name.lower()}
            if jar_keys & set(keys):
                matches.append(j)
        if not matches:
            raise KeyError(
                f"no jar for agent={agent_id!r} matching repo keys {keys}; "
                f"set repos on the jar or pass jar= explicitly"
            )
        if len(matches) > 1:
            # prefer longest key overlap (remote over basename)
            def score(j: Jar) -> int:
                return max((len(k) for k in (set(j.repos) | {j.name.lower()}) & set(keys)), default=0)

            matches.sort(key=score, reverse=True)
            if score(matches[0]) == score(matches[1]):
                names = ", ".join(m.name for m in matches)
                raise ValueError(f"ambiguous jars for {keys}: {names} — pass jar= explicitly")
        return matches[0]

    def get_jar(self, jar_id_or_name: str) -> Jar | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jars WHERE id = %s OR name = %s",
                (jar_id_or_name, jar_id_or_name),
            ).fetchone()
        return self._row_to_jar(row) if row else None

    def list_jars(self, agent_id: str | None = None, include_archived: bool = False) -> list[Jar]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM jars ORDER BY created_at DESC").fetchall()
        jars = [self._row_to_jar(r) for r in rows]
        if not include_archived:
            jars = [j for j in jars if not j.archived]
        if agent_id:
            jars = [j for j in jars if agent_id in j.agents]
        return jars

    def set_jar_paused(self, jar_id_or_name: str, paused: bool) -> Jar:
        jar = self.get_jar(jar_id_or_name)
        if not jar:
            raise KeyError(f"jar not found: {jar_id_or_name}")
        with self._pool.connection() as conn:
            conn.execute("UPDATE jars SET paused = %s WHERE id = %s", (paused, jar.id))
            conn.commit()
        jar.paused = paused
        return jar

    def update_agent_local(self, jar_id: str, state: AgentLocalState) -> Jar:
        jar = self.get_jar(jar_id)
        if not jar:
            raise KeyError(f"jar not found: {jar_id}")
        if state.agent_id not in jar.agents:
            raise KeyError(f"agent {state.agent_id} not on jar {jar_id}")
        jar.local[state.agent_id] = state
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE jars SET local_json = %s::jsonb WHERE id = %s",
                (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
            )
            conn.commit()
        return jar

    def attach_agent(self, jar_id_or_name: str, agent_id: str) -> Jar:
        jar = self.get_jar(jar_id_or_name)
        if not jar:
            raise KeyError(f"jar not found: {jar_id_or_name}")
        if agent_id not in jar.agents:
            jar.agents.append(agent_id)
            jar.local[agent_id] = AgentLocalState(agent_id=agent_id)
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE jars
                    SET agents_json = %s::jsonb, local_json = %s::jsonb
                    WHERE id = %s
                    """,
                    (
                        json.dumps(jar.agents),
                        json.dumps({k: v.model_dump() for k, v in jar.local.items()}),
                        jar.id,
                    ),
                )
                conn.commit()
        return jar

    def add_repos(self, jar_id_or_name: str, repos: list[str]) -> Jar:
        from messjar.repo import normalize_repo_key

        jar = self.get_jar(jar_id_or_name)
        if not jar:
            raise KeyError(f"jar not found: {jar_id_or_name}")
        changed = False
        for raw in repos:
            key = normalize_repo_key(raw)
            if key and key not in jar.repos:
                jar.repos.append(key)
                changed = True
        if changed:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE jars SET repos_json = %s::jsonb WHERE id = %s",
                    (json.dumps(jar.repos), jar.id),
                )
                conn.commit()
        return jar

    def send(self, mess: Mess) -> Mess:
        jar = self.get_jar(mess.jar_id)
        if not jar:
            raise KeyError(f"jar not found: {mess.jar_id}")
        if jar.paused:
            raise RuntimeError(f"jar {jar.name} is paused")
        if mess.from_agent not in jar.agents or mess.to_agent not in jar.agents:
            raise ValueError("from/to must be participants on the jar")
        mess.jar_id = jar.id
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT id FROM jars WHERE id = %s FOR UPDATE", (jar.id,))
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS m FROM messes WHERE jar_id = %s",
                    (jar.id,),
                ).fetchone()
                seq = int(row["m"]) + 1
                mess.seq = seq
                conn.execute(
                    """
                    INSERT INTO messes (
                        id, jar_id, seq, from_agent, to_agent, body, kind,
                        reply_expected, hop, refs_json, ts, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        mess.id,
                        mess.jar_id,
                        seq,
                        mess.from_agent,
                        mess.to_agent,
                        mess.body,
                        mess.kind.value,
                        mess.reply_expected,
                        mess.hop,
                        json.dumps(mess.refs),
                        mess.ts,
                        mess.schema_version,
                    ),
                )
                sender = jar.local.get(mess.from_agent) or AgentLocalState(agent_id=mess.from_agent)
                sender.hop = max(sender.hop, mess.hop)
                jar.local[mess.from_agent] = sender
                conn.execute(
                    "UPDATE jars SET local_json = %s::jsonb WHERE id = %s",
                    (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
                )
        return mess

    def list_messes(
        self,
        jar_id_or_name: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        to_agent: str | None = None,
    ) -> list[Mess]:
        jar = self.get_jar(jar_id_or_name)
        if not jar:
            raise KeyError(f"jar not found: {jar_id_or_name}")
        q = "SELECT * FROM messes WHERE jar_id = %s AND seq > %s"
        params: list[Any] = [jar.id, after_seq]
        if to_agent:
            q += " AND to_agent = %s"
            params.append(to_agent)
        q += " ORDER BY seq ASC LIMIT %s"
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_mess(r) for r in rows]

    def check_jar(
        self,
        agent_id: str,
        jar_id_or_name: str | None = None,
        *,
        after_seq: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        jars = [self.get_jar(jar_id_or_name)] if jar_id_or_name else self.list_jars(agent_id)
        jars = [j for j in jars if j and agent_id in j.agents and not j.archived]
        out: list[dict[str, Any]] = []
        for jar in jars:
            assert jar is not None
            local = jar.local.get(agent_id) or AgentLocalState(agent_id=agent_id)
            if jar.paused or local.paused:
                continue
            cursor = after_seq if after_seq is not None else local.cursor
            messes = self.list_messes(jar.id, after_seq=cursor, limit=limit, to_agent=agent_id)
            if not messes:
                continue
            out.append(
                {
                    "jar": jar.model_dump(by_alias=True),
                    "messes": [m.model_dump(by_alias=True) for m in messes],
                    "cursor": messes[-1].seq,
                }
            )
        return out

    def advance_cursor(self, jar_id: str, agent_id: str, seq: int) -> None:
        jar = self.get_jar(jar_id)
        if not jar:
            raise KeyError(f"jar not found: {jar_id}")
        if agent_id not in jar.agents:
            raise KeyError(f"agent {agent_id} not on jar")
        local = jar.local.get(agent_id) or AgentLocalState(agent_id=agent_id)
        local.cursor = max(local.cursor, seq)
        jar.local[agent_id] = local
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE jars SET local_json = %s::jsonb WHERE id = %s",
                (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
            )
            conn.commit()

    def wait(
        self,
        agent_id: str,
        *,
        jar_id_or_name: str | None = None,
        after_seq: int | None = None,
        timeout_s: float = 30.0,
        poll_s: float = 0.5,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_s
        while True:
            batch = self.check_jar(agent_id, jar_id_or_name, after_seq=after_seq)
            if batch:
                return batch
            if time.monotonic() >= deadline:
                return []
            time.sleep(poll_s)

    def _as_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value)
        return value

    def _row_to_jar(self, row: dict[str, Any]) -> Jar:
        local_raw = self._as_json(row["local_json"])
        local = {k: AgentLocalState.model_validate(v) for k, v in local_raw.items()}
        created = row["created_at"]
        created_at = created if isinstance(created, str) else created.strftime("%Y-%m-%dT%H:%M:%SZ")
        return Jar(
            id=row["id"],
            name=row["name"],
            agents=list(self._as_json(row["agents_json"])),
            local=local,
            paused=bool(row["paused"]),
            archived=bool(row["archived"]),
            created_at=created_at,
            meta=dict(self._as_json(row["meta_json"] or {})),
            password=row.get("password"),
            repos=list(self._as_json(row.get("repos_json") or [])),
        )

    def _row_to_mess(self, row: dict[str, Any]) -> Mess:
        ts = row["ts"]
        ts_s = ts if isinstance(ts, str) else ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        return Mess(
            id=row["id"],
            jar_id=row["jar_id"],
            **{"from": row["from_agent"], "to": row["to_agent"]},
            body=row["body"],
            kind=MessKind(row["kind"]),
            reply_expected=bool(row["reply_expected"]),
            hop=row["hop"],
            refs=list(self._as_json(row["refs_json"])),
            ts=ts_s,
            schema_version=row["schema_version"],
            seq=row["seq"],
        )


def database_host_label(url: str) -> str:
    try:
        p = urlparse(normalize_database_url(url))
        return p.hostname or "postgres"
    except Exception:
        return "postgres"
