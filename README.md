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

1. **Bus** — MCP/HTTP server plus **PostgreSQL**. Tools: `send`, `check_jar`, `wait`, `list_jars`. Commodity plumbing.
2. **Daemon** — the product. Adapters (`claude_code`, `cursor`, …) turn a Mess into a headless tool run and track session ids.
3. **Envelope schema** — the versioned protocol shared by bus and daemon.
4. **CLI** (`mj`) — `mj jars`, `mj tail <jar>`, `mj pause`.

## Auth model (share with your friend)

You share **two things**, same on both sides:

1. **HTTPS URL** of the Railway bus — e.g. `https://mess-jar-production.up.railway.app`
2. **Shared password** — one secret (`MESSJAR_PASSWORD`) both of you put in MCP config / CLI / daemon

Not per-user accounts. Same link + same password → both agents can use the bus; jars still scope who can talk (`agents` list).

Clients send:

```http
Authorization: Bearer <shared-password>
```

(`/health` stays open for Railway healthchecks.)

## Quick start (local)

```bash
python3 -m virtualenv .venv && source .venv/bin/activate
pip install -e .

# Postgres + bus
docker compose up -d db
export DATABASE_URL=postgresql://messjar:messjar@127.0.0.1:5434/messjar
export MESSJAR_PASSWORD=devpassword
mj bus serve --host 127.0.0.1 --port 7420

# other terminal
export MESSJAR_BUS=http://127.0.0.1:7420
export MESSJAR_PASSWORD=devpassword
mj jars create collab-auth --agents alice@cursor,bob@claude
mj send collab-auth --from alice@cursor --to bob@claude \
  --kind question --body "Can your service accept this OpenAPI diff?"
mj daemon run --agent bob@claude --adapter claude_code --workdir ~/src/bob-api --dry-run
mj tail collab-auth
```

Or all-in-one: `docker compose up --build`

## Deploy on Railway

1. Deploy this repo (Dockerfile).
2. Add **PostgreSQL** plugin → Railway sets `DATABASE_URL`.
3. Set `MESSJAR_PASSWORD` to a long shared secret (and keep `MESSJAR_REQUIRE_AUTH=1`).
4. Share with your friend: **HTTPS URL + password**.

```bash
export MESSJAR_BUS=https://YOUR-APP.up.railway.app
export MESSJAR_PASSWORD='the-shared-secret'
mj jars create collab-auth --agents alice@cursor,bob@claude
mj daemon run --agent bob@claude --adapter claude_code --workdir ~/src/api
```

### MCP config (both sides)

See [`examples/mcp.cursor.json`](examples/mcp.cursor.json):

```json
{
  "mcpServers": {
    "messjar": {
      "url": "https://YOUR-APP.up.railway.app/mcp",
      "headers": {
        "Authorization": "Bearer SHARED_PASSWORD_BOTH_SIDES_USE"
      }
    }
  }
}
```

MCP endpoints: `GET /mcp` (tool list), `POST /mcp/call`.

## MCP tools

| Tool | Purpose |
|------|---------|
| `send` | Post a Mess into a Jar |
| `check_jar` | Non-blocking: new messes for this agent |
| `wait` | Block until a Mess arrives (or timeout) |
| `list_jars` | Jars this agent is attached to |

## Env vars

| Var | Where | Purpose |
|-----|--------|---------|
| `DATABASE_URL` | bus | Postgres connection string |
| `MESSJAR_PASSWORD` | bus + clients | Shared password (both sides) |
| `MESSJAR_REQUIRE_AUTH` | bus | Fail boot if password unset (`1` in Docker) |
| `MESSJAR_BUS` | clients | HTTPS bus URL |
| `PORT` / `HOST` | bus | Bind (Railway sets `PORT`) |

## Layout

```
messjar/
  schema.py
  store.py       # PostgreSQL
  auth.py        # shared password gate
  bus/server.py  # HTTP + /mcp
  daemon/        # adapters
  cli.py
examples/mcp.cursor.json
Dockerfile
docker-compose.yml
```

## Status

MVP: Postgres bus, shared-password auth, MCP HTTP endpoints, polling daemon with adapters, CLI. Adapters invoke real tools when binaries exist; otherwise dry-run.
