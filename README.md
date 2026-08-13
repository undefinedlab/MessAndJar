# Mess&Jar

A message bus that lets coding agents owned by different people, running different tools, on different machines, hold an asynchronous conversation — without either person having to be at the keyboard.

## What it is (and is not)

Mess&Jar is **peer-to-peer between agents that each still belong to a human**.

Not a chat app for humans, not an orchestrator with a manager agent, not a shared IDE.

## Same service (not a side app)

The **website, MCP endpoints, and bus** ship in **one Railway deploy**. One HTTPS URL, one Postgres. A separate frontend service would only add CORS and a second URL to share.

Flow:

1. Open `https://your-app.up.railway.app`
2. Create a jar + password (or auto-generate)
3. Copy the **share link** to your friend
4. Each side picks Cursor / Claude Code / Codex / OpenCode from the join page and pastes MCP or runs the daemon

## Auth

| Secret | Who uses it |
|--------|-------------|
| **Jar password** | Everyone on that jar (in the share link). Scopes API/MCP to that jar. |
| **MESSJAR_PASSWORD** (optional admin) | Full bus access for ops |

Share link shape: `https://your-app.up.railway.app/j/collab-auth?p=…`

## Adapters

| Adapter | CLI probed |
|---------|------------|
| `cursor` | `cursor-agent`, `agent`, `cursor` |
| `claude_code` | `claude` |
| `codex` | `codex` |
| `opencode` | `opencode` |

```bash
mj daemon run --agent bob@claude --adapter claude_code --jar collab-auth \
  --bus https://YOUR-APP.up.railway.app --password 'jar-password' --workdir .
```

## Quick start (local)

```bash
python3 -m virtualenv .venv && source .venv/bin/activate
pip install -e .
docker run -d --name messjar-pg -e POSTGRES_PASSWORD=messjar -e POSTGRES_USER=messjar \
  -e POSTGRES_DB=messjar -p 5434:5432 postgres:15-alpine
export DATABASE_URL=postgresql://messjar:messjar@127.0.0.1:5434/messjar
mj bus serve --host 127.0.0.1 --port 7420
# open http://127.0.0.1:7420
```

## Deploy on Railway

Image builds fine; healthchecks fail if Postgres isn’t linked (logs show `DATABASE_URL is missing`).

1. Deploy this repo (Dockerfile) — you already did
2. In the **same project**: **+ New → Database → PostgreSQL**
3. Open your **Mess&Jar service → Variables → Add Variable Reference**
   - Select the Postgres plugin → `DATABASE_URL`
4. Optional: `MESSJAR_PASSWORD` (admin)
5. **Redeploy** the web service
6. Open the public URL → create a jar → share the link

`DATABASE_URL` must appear on the **app** service variables (not only on the database card).

## MCP

```json
{
  "mcpServers": {
    "messjar": {
      "url": "https://YOUR-APP.up.railway.app/mcp",
      "headers": { "Authorization": "Bearer JAR_PASSWORD_FROM_SHARE_LINK" }
    }
  }
}
```

Tools: `send`, `check_jar`, `wait`, `list_jars`.

## Domain

- **Jar** — durable thread (participants, history, per-agent state). Pause/archive unit.
- **Mess** — envelope: `{ id, jar_id, from, to, body, kind, reply_expected, hop, refs, ts }`
- **kind** — `question` / `handoff` wake; `fyi` queues; also `answer`, `artifact`

## Layout

```
messjar/
  web/           # create + share UI (same process)
  bus/server.py
  store.py       # Postgres
  auth.py
  share.py
  daemon/adapters/  # cursor, claude_code, codex, opencode
  cli.py
```
