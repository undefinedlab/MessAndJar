"""SQLite persistence for Jars and Messes."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from messjar.schema import AgentLocalState, Jar, Mess, MessKind


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jars (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    agents_json TEXT NOT NULL,
                    local_json TEXT NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messes (
                    id TEXT PRIMARY KEY,
                    jar_id TEXT NOT NULL REFERENCES jars(id),
                    seq INTEGER NOT NULL,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reply_expected INTEGER NOT NULL,
                    hop INTEGER NOT NULL,
                    refs_json TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    UNIQUE(jar_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_messes_jar_seq ON messes(jar_id, seq);
                CREATE INDEX IF NOT EXISTS idx_messes_to ON messes(to_agent, jar_id, seq);
                """
            )
            self._conn.commit()

    # --- jars ---

    def create_jar(self, name: str, agents: list[str], meta: dict[str, Any] | None = None) -> Jar:
        jar = Jar.create(name=name, agents=agents, meta=meta)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jars (id, name, agents_json, local_json, paused, archived, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    jar.id,
                    jar.name,
                    json.dumps(jar.agents),
                    json.dumps({k: v.model_dump() for k, v in jar.local.items()}),
                    int(jar.paused),
                    int(jar.archived),
                    jar.created_at,
                    json.dumps(jar.meta),
                ),
            )
            self._conn.commit()
        return jar

    def get_jar(self, jar_id_or_name: str) -> Jar | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jars WHERE id = ? OR name = ?",
                (jar_id_or_name, jar_id_or_name),
            ).fetchone()
        return self._row_to_jar(row) if row else None

    def list_jars(self, agent_id: str | None = None, include_archived: bool = False) -> list[Jar]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jars ORDER BY created_at DESC").fetchall()
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
        with self._lock:
            self._conn.execute(
                "UPDATE jars SET paused = ? WHERE id = ?",
                (int(paused), jar.id),
            )
            self._conn.commit()
        jar.paused = paused
        return jar

    def update_agent_local(self, jar_id: str, state: AgentLocalState) -> Jar:
        jar = self.get_jar(jar_id)
        if not jar:
            raise KeyError(f"jar not found: {jar_id}")
        if state.agent_id not in jar.agents:
            raise KeyError(f"agent {state.agent_id} not on jar {jar_id}")
        jar.local[state.agent_id] = state
        with self._lock:
            self._conn.execute(
                "UPDATE jars SET local_json = ? WHERE id = ?",
                (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
            )
            self._conn.commit()
        return jar

    def attach_agent(self, jar_id_or_name: str, agent_id: str) -> Jar:
        jar = self.get_jar(jar_id_or_name)
        if not jar:
            raise KeyError(f"jar not found: {jar_id_or_name}")
        if agent_id not in jar.agents:
            jar.agents.append(agent_id)
            jar.local[agent_id] = AgentLocalState(agent_id=agent_id)
            with self._lock:
                self._conn.execute(
                    "UPDATE jars SET agents_json = ?, local_json = ? WHERE id = ?",
                    (
                        json.dumps(jar.agents),
                        json.dumps({k: v.model_dump() for k, v in jar.local.items()}),
                        jar.id,
                    ),
                )
                self._conn.commit()
        return jar

    # --- messes ---

    def send(self, mess: Mess) -> Mess:
        jar = self.get_jar(mess.jar_id)
        if not jar:
            raise KeyError(f"jar not found: {mess.jar_id}")
        if jar.paused:
            raise RuntimeError(f"jar {jar.name} is paused")
        if mess.from_agent not in jar.agents or mess.to_agent not in jar.agents:
            raise ValueError("from/to must be participants on the jar")
        mess.jar_id = jar.id
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM messes WHERE jar_id = ?",
                (jar.id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            mess.seq = seq
            self._conn.execute(
                """
                INSERT INTO messes (
                    id, jar_id, seq, from_agent, to_agent, body, kind,
                    reply_expected, hop, refs_json, ts, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mess.id,
                    mess.jar_id,
                    seq,
                    mess.from_agent,
                    mess.to_agent,
                    mess.body,
                    mess.kind.value,
                    int(mess.reply_expected),
                    mess.hop,
                    json.dumps(mess.refs),
                    mess.ts,
                    mess.schema_version,
                ),
            )
            # bump sender hop tracking lightly
            sender = jar.local.get(mess.from_agent) or AgentLocalState(agent_id=mess.from_agent)
            sender.hop = max(sender.hop, mess.hop)
            jar.local[mess.from_agent] = sender
            self._conn.execute(
                "UPDATE jars SET local_json = ? WHERE id = ?",
                (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
            )
            self._conn.commit()
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
        q = "SELECT * FROM messes WHERE jar_id = ? AND seq > ?"
        params: list[Any] = [jar.id, after_seq]
        if to_agent:
            q += " AND to_agent = ?"
            params.append(to_agent)
        q += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_mess(r) for r in rows]

    def check_jar(
        self,
        agent_id: str,
        jar_id_or_name: str | None = None,
        *,
        after_seq: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Non-blocking: unread messes for agent, optionally scoped to one jar."""
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
        local = jar.local.get(agent_id) or AgentLocalState(agent_id=agent_id)
        local.cursor = max(local.cursor, seq)
        if jar.local.get(agent_id) is None:
            # agent might only be addressed; still allow cursor if participant
            if agent_id not in jar.agents:
                raise KeyError(f"agent {agent_id} not on jar")
        jar.local[agent_id] = local
        with self._lock:
            self._conn.execute(
                "UPDATE jars SET local_json = ? WHERE id = ?",
                (json.dumps({k: v.model_dump() for k, v in jar.local.items()}), jar.id),
            )
            self._conn.commit()

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

    # --- helpers ---

    def _row_to_jar(self, row: sqlite3.Row) -> Jar:
        local_raw = json.loads(row["local_json"])
        local = {k: AgentLocalState.model_validate(v) for k, v in local_raw.items()}
        return Jar(
            id=row["id"],
            name=row["name"],
            agents=json.loads(row["agents_json"]),
            local=local,
            paused=bool(row["paused"]),
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            meta=json.loads(row["meta_json"] or "{}"),
        )

    def _row_to_mess(self, row: sqlite3.Row) -> Mess:
        return Mess(
            id=row["id"],
            jar_id=row["jar_id"],
            **{"from": row["from_agent"], "to": row["to_agent"]},
            body=row["body"],
            kind=MessKind(row["kind"]),
            reply_expected=bool(row["reply_expected"]),
            hop=row["hop"],
            refs=json.loads(row["refs_json"]),
            ts=row["ts"],
            schema_version=row["schema_version"],
            seq=row["seq"],
        )
