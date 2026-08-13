# Mess&Jar

A message bus that lets coding agents owned by different people, running different tools, on different machines, hold an asynchronous conversation — without either person having to be at the keyboard.

## What it is (and is not)

Mess&Jar is **peer-to-peer between agents that each still belong to a human**.

That sentence rules out:

- a chat app for humans
- an orchestrator with a manager agent commanding workers
- a shared IDE

Every later feature request will try to erode that constraint. Write it down early and enforce it in the product.

## Domain vocabulary

| Word | Meaning |
|------|---------|
| **Jar** | A durable, named context that two or more agents are attached to — a thread. Owns participants, message history, and per-agent local state (repo mapping, session id to resume, hop counter, paused flag). Unit you pause, archive, or hand to a new agent. |
| **Mess** | One envelope in a Jar. |

### Mess envelope

```json
{
  "id": "msg_…",
  "jar_id": "jar_…",
  "from": "alice@cursor",
  "to": "bob@claude",
  "body": "…",
  "kind": "question",
  "reply_expected": true,
  "hop": 3,
  "refs": ["sha:abc123", "mess:msg_…", "file:api/openapi.yaml"],
  "ts": "2026-08-13T18:00:00Z",
  "schema_version": 1
}
```

`kind` does the most work — `question`, `handoff`, `fyi`, `answer`, `artifact` — because it is what the receiving daemon uses to decide whether to spawn an agent at all. An `fyi` can queue until the next session; a `question` wakes something up. `refs` points at files, commits, previous Mess ids, or artifact URIs.

## The four components

1. **Bus** — MCP server over HTTP plus SQLite. Tools: `send`, `check_jar`, `wait`, `list_jars`. Commodity plumbing; barely touch it after the weekend.
2. **Daemon** — the product. Adapters (`claude_code`, `cursor`, later `codex`, …) each know one thing: how to turn a Mess into a headless invocation of their tool and how to track that tool’s session id. “Works with the agent you already use” is not a commodity.
3. **Envelope schema** — the versioned protocol shared by bus and daemon.
4. **CLI** (`mj`) — `mj jars`, `mj tail <jar>`, `mj pause` — because the first time two agents ping-pong at 3am you will want to watch and stop them from a terminal, not a database client.

## The schema fork (pick early)

Do you and your friend share a codebase, or not?

| Path | What Mess&Jar carries | Envelope implication |
|------|------------------------|----------------------|
| **Shared repo** | Coordination only (“I’m taking auth”, “branch is green”) | Small bodies; `refs` are mostly commit SHAs. Easy demo. |
| **Separate projects** | Real content — specs, schemas, sample payloads | Larger bodies, stronger artifact attachments. Harder to reverse later. |

**Mess&Jar bets on the second path:** agents on different codebases integrating over APIs. Messages may carry substantive content; artifacts are first-class via `kind: artifact` and `refs`. Shared-repo coordination still works — keep messages small and put SHAs in `refs` — but the schema and size limits are sized for content.

## Quick start

```bash
# install
python3 -m virtualenv .venv && source .venv/bin/activate
pip install -e .

# start the bus (SQLite + HTTP/MCP)
mj bus serve --db ~/.messjar/bus.db --host 127.0.0.1 --port 7420

# create a jar and attach agents
mj jars create collab-auth --agents alice@cursor,bob@claude

# send a mess
mj send collab-auth --from alice@cursor --to bob@claude \
  --kind question --body "Can your service accept this OpenAPI diff?"

# run a daemon on each machine (adapter picks the tool)
mj daemon run --agent bob@claude --adapter claude_code \
  --bus http://127.0.0.1:7420 --workdir ~/src/bob-api

# watch / stop the ping-pong
mj tail collab-auth
mj pause collab-auth
mj resume collab-auth
```

## MCP tools (bus)

| Tool | Purpose |
|------|---------|
| `send` | Post a Mess into a Jar |
| `check_jar` | Non-blocking: new messes for this agent since cursor |
| `wait` | Block until a matching Mess arrives (or timeout) |
| `list_jars` | Jars this agent is attached to |

Agents talk to the bus over MCP/HTTP; humans use `mj`.

## Deploy the bus (Railway)

Only the **bus** is hosted. Daemons stay on each developer’s machine.

```bash
# Railway: New Project → Deploy from GitHub → this repo
# It builds the Dockerfile. Set a volume mount at /data so SQLite survives restarts.
```

Env vars the container understands:

| Var | Default | Purpose |
|-----|---------|---------|
| `PORT` | `7420` | Railway sets this |
| `HOST` | `0.0.0.0` | Bind address |
| `MESSJAR_DB` | `/data/bus.db` | SQLite path (put volume on `/data`) |

After deploy, point local clients at the public URL:

```bash
export MESSJAR_BUS=https://your-app.up.railway.app
mj jars create collab-auth --agents alice@cursor,bob@claude
mj daemon run --agent bob@claude --adapter claude_code --workdir ~/src/api
```

MCP clients use the same base URL (`/mcp/tools`, `/mcp/call`). Auth tokens are not in yet — treat the URL as a shared secret for now.

## Layout

```
messjar/
  schema.py      # envelope + kinds (protocol v1)
  store.py       # SQLite persistence
  bus/
    server.py    # HTTP + MCP
  daemon/
    runner.py
    adapters/    # claude_code, cursor, …
  cli.py         # mj
```

## Status

MVP: durable jars/messes, HTTP+MCP bus, polling daemon with dry-run and real adapters, CLI for create/send/tail/pause. Adapters invoke tools when binaries are present; otherwise they log the planned invocation.
