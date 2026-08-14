"""mj — Mess&Jar CLI."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from messjar.client import BusClient

app = typer.Typer(
    name="mj",
    help="Mess&Jar — peer async bus for coding agents.",
    no_args_is_help=True,
)
jars_app = typer.Typer(help="Manage jars")
bus_app = typer.Typer(help="Run the bus")
daemon_app = typer.Typer(help="Run a local agent daemon")
label_app = typer.Typer(help="Propose and review changes to a jar's label")
held_app = typer.Typer(help="Review handoff/artifact messages held for your approval")
app.add_typer(jars_app, name="jars")
app.add_typer(bus_app, name="bus")
app.add_typer(daemon_app, name="daemon")
app.add_typer(label_app, name="label")
app.add_typer(held_app, name="held")

console = Console()
DEFAULT_BUS = os.environ.get("MESSJAR_BUS", "http://127.0.0.1:7420")
DEFAULT_PASSWORD = os.environ.get("MESSJAR_PASSWORD") or os.environ.get("MESSJAR_TOKEN")


def _client(bus: str, password: Optional[str] = None) -> BusClient:
    return BusClient(bus, password=password if password is not None else DEFAULT_PASSWORD)


@bus_app.command("serve")
def bus_serve(
    database_url: Optional[str] = typer.Option(
        None,
        "--database-url",
        envvar="DATABASE_URL",
        help="PostgreSQL URL (required)",
    ),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """Start the HTTP/MCP bus (Postgres + shared password)."""
    from messjar.bus.server import run_server

    bind_host = host or os.environ.get("HOST", "0.0.0.0")
    bind_port = port or int(os.environ.get("PORT", "7420"))
    run_server(database_url, host=bind_host, port=bind_port)


@jars_app.command("create")
def jars_create(
    name: str = typer.Argument(..., help="Jar name"),
    agents: Optional[str] = typer.Option(
        None, "--agents", help="Optional pre-seeded agent ids (usually join via share link)"
    ),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    agent_list = [a.strip() for a in (agents or "").split(",") if a.strip()]
    with _client(bus, password) as c:
        jar = c.create_jar(name, agent_list)
    console.print(f"created [cyan]{jar['name']}[/] id={jar['id']} agents={jar['agents']}")


@jars_app.command("list")
def jars_list(
    agent: Optional[str] = typer.Option(None, "--agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    with _client(bus, password) as c:
        jars = c.list_jars(agent)
    table = Table(title="Jars")
    table.add_column("name")
    table.add_column("id")
    table.add_column("agents")
    table.add_column("paused")
    for j in jars:
        table.add_row(
            j["name"],
            j["id"],
            ", ".join(j["agents"]),
            "yes" if j.get("paused") else "no",
        )
    console.print(table)


@jars_app.callback(invoke_without_command=True)
def jars_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        jars_list()


@app.command("send")
def send_mess(
    jar: str = typer.Argument(..., help="Jar name or id"),
    from_agent: str = typer.Option(..., "--from", help="Sender agent id"),
    to_agent: str = typer.Option(..., "--to", help="Receiver agent id"),
    kind: str = typer.Option("fyi", "--kind"),
    body: Optional[str] = typer.Option(None, "--body"),
    body_file: Optional[Path] = typer.Option(None, "--body-file"),
    hop: int = typer.Option(0, "--hop"),
    refs: Optional[str] = typer.Option(None, "--refs", help="Comma-separated refs"),
    trigger_source: str = typer.Option(
        "human", "--trigger-source", help="human (default) or agent — agent-triggered "
        "handoff/artifact gets held for the sender's human, per the jar's policy"
    ),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    """Post a Mess into a Jar."""
    if body is None and body_file is None:
        if not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            raise typer.BadParameter("provide --body, --body-file, or stdin")
    if body_file is not None:
        body = body_file.read_text()
    assert body is not None
    ref_list = [r.strip() for r in (refs or "").split(",") if r.strip()]
    with _client(bus, password) as c:
        mess = c.send(
            jar,
            from_agent=from_agent,
            to_agent=to_agent,
            body=body,
            kind=kind,
            hop=hop,
            refs=ref_list,
            trigger_source=trigger_source,
        )
    if mess.get("status") == "held":
        console.print(
            f"held [yellow]{mess['id']}[/] kind={mess['kind']} → {mess['to_agent']} "
            f"— queued for the sender's human, review with `mj held list {jar}`"
        )
    else:
        console.print(
            f"sent [green]{mess['id']}[/] kind={mess['kind']} seq={mess.get('seq')} → {mess['to']}"
        )


@app.command("tail")
def tail_jar(
    jar: str = typer.Argument(...),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    """Watch Messes in a Jar."""
    with _client(bus, password) as c:
        after = 0
        while True:
            messes = c.messes(jar, after_seq=after, limit=100)
            for m in messes:
                console.print(
                    f"[dim]{m['ts']}[/] [cyan]#{m.get('seq')}[/] "
                    f"[yellow]{m['kind']}[/] {m['from']} → {m['to']}  "
                    f"[dim]{m['id']}[/]"
                )
                console.print(m["body"], markup=False)
                console.print()
                after = max(after, int(m.get("seq") or 0))
            if not follow:
                break
            time.sleep(1.0)


@app.command("pause")
def pause_jar(
    jar: Optional[str] = typer.Argument(None),
    all: bool = typer.Option(
        False,
        "--all",
        help="Local kill switch: pauses every daemon on this machine. No network/DB.",
    ),
    reason: Optional[str] = typer.Option(None, "--reason"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    if all:
        from messjar.localstate import engage_killswitch

        state = engage_killswitch(reason)
        suffix = f" ({state['reason']})" if state.get("reason") else ""
        console.print(f"local kill switch engaged at {state['paused_at']}{suffix}")
        return
    if not jar:
        raise typer.BadParameter("jar name required unless --all")
    with _client(bus, password) as c:
        j = c.pause(jar)
    reason_suffix = f" reason={j['paused_reason']}" if j.get("paused_reason") else ""
    console.print(f"paused [cyan]{j['name']}[/]{reason_suffix}")


@app.command("resume")
def resume_jar(
    jar: Optional[str] = typer.Argument(None),
    all: bool = typer.Option(
        False, "--all", help="Release the local kill switch. No network/DB."
    ),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    if all:
        from messjar.localstate import release_killswitch

        was_engaged = release_killswitch()
        console.print(
            "local kill switch released" if was_engaged else "local kill switch was not engaged"
        )
        return
    if not jar:
        raise typer.BadParameter("jar name required unless --all")
    with _client(bus, password) as c:
        j = c.resume(jar)
    console.print(f"resumed [cyan]{j['name']}[/]")


def _age_str(ts: str) -> str:
    from datetime import datetime, timezone

    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _loop_line(m: dict) -> str:
    preview = (m.get("body") or "").strip().replace("\n", " ")
    if len(preview) > 60:
        preview = preview[:57] + "..."
    return f"[dim]{_age_str(m.get('ts', ''))} ago[/] {m.get('kind')} {m.get('from')} → {m.get('to')}: {preview}"


@app.command("digest")
def digest_cmd(
    jar: Optional[str] = typer.Argument(None, help="Limit to one jar (default: every jar)"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Show what's waiting on this agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
) -> None:
    """What moved, what's open, what's waiting on you — across every jar. Your re-entry point."""
    with _client(bus, password) as c:
        entries = c.digest(jar=jar, agent=agent)
    if not entries:
        console.print("[dim]nothing to show[/]")
        return
    for entry in entries:
        console.print(f"[bold cyan]{entry['jar']}[/]")
        waiting = entry.get("waiting_on_you") or []
        waiting_ids = {m["id"] for m in waiting}
        if waiting:
            console.print(f"  [bold yellow]waiting on you ({len(waiting)})[/]")
            for m in waiting:
                console.print(f"    {_loop_line(m)}")
        open_loops = [m for m in (entry.get("open_loops") or []) if m["id"] not in waiting_ids]
        if open_loops:
            console.print(f"  open ({len(open_loops)})")
            for m in open_loops:
                console.print(f"    {_loop_line(m)}")
        stale = entry.get("stale_loops") or []
        if stale:
            console.print(f"  [red]stale ({len(stale)})[/]")
            for m in stale:
                console.print(f"    {_loop_line(m)}")
        recent = entry.get("recent") or []
        if recent:
            console.print("  recent:")
            for m in recent:
                console.print(
                    f"    [dim]{_age_str(m.get('ts', ''))} ago[/] {m['kind']} {m['from']} → {m['to']}"
                )
        if not waiting and not open_loops and not stale and not recent:
            console.print("  [dim](quiet)[/]")
        console.print()


def _read_body(body: Optional[str], body_file: Optional[Path]) -> str:
    if body is not None:
        return body
    if body_file is not None:
        return body_file.read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise typer.BadParameter("provide --body, --body-file, or stdin")


@label_app.command("show")
def label_show(
    jar: str = typer.Argument(...),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        j = c.get_jar(jar)
    label = j.get("label")
    if label:
        console.print(label, markup=False)
    else:
        console.print("[dim](no label set)[/]")


@label_app.command("propose")
def label_propose(
    jar: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent"),
    body: Optional[str] = typer.Option(None, "--body"),
    body_file: Optional[Path] = typer.Option(None, "--body-file"),
    origin_mess: Optional[str] = typer.Option(None, "--origin-mess"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    """Propose new label text. Takes effect only once every participant accepts."""
    patch = _read_body(body, body_file)
    with _client(bus, password) as c:
        proposal = c.propose_label(jar, agent=agent, patch=patch, origin_mess_id=origin_mess)
    console.print(f"proposed [cyan]{proposal['id']}[/] on {jar} — awaiting acceptance from all participants")


@label_app.command("list")
def label_list(
    jar: str = typer.Argument(...),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        proposals = c.list_label_proposals(jar, status="pending")
    if not proposals:
        console.print("[dim]no pending proposals[/]")
        return
    for p in proposals:
        console.print(
            f"[cyan]{p['id']}[/] by {p['proposed_by']} — accepted by: {', '.join(p['accepted_by']) or '(none)'}"
        )
        console.print(p["diff"] or "(empty diff)", markup=False)
        console.print()


@label_app.command("accept")
def label_accept(
    jar: str = typer.Argument(...),
    proposal_id: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        p = c.decide_label_proposal(jar, proposal_id, agent=agent, decision="accept")
    console.print(f"[cyan]{p['id']}[/] status={p['status']} accepted_by={p['accepted_by']}")


@label_app.command("reject")
def label_reject(
    jar: str = typer.Argument(...),
    proposal_id: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        p = c.decide_label_proposal(jar, proposal_id, agent=agent, decision="reject")
    console.print(f"[cyan]{p['id']}[/] status={p['status']}")


@label_app.command("edit")
def label_edit(
    jar: str = typer.Argument(...),
    proposal_id: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent"),
    body: Optional[str] = typer.Option(None, "--body"),
    body_file: Optional[Path] = typer.Option(None, "--body-file"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    """Replace the proposed text and accept it — resets everyone else's prior accept."""
    patch = _read_body(body, body_file)
    with _client(bus, password) as c:
        p = c.edit_label_proposal(jar, proposal_id, agent=agent, patch=patch)
    console.print(f"[cyan]{p['id']}[/] edited + accepted by {agent}; status={p['status']}")


@held_app.command("list")
def held_list(
    jar: str = typer.Argument(...),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Only messages held from this sender"
    ),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        held = c.list_held(jar, from_agent=agent)
    if not held:
        console.print("[dim]nothing held[/]")
        return
    for h in held:
        console.print(
            f"[cyan]{h['id']}[/] {h['kind']} {h['from_agent']} → {h['to_agent']} "
            f"(held until {h['held_until']})"
        )
        console.print(h["body"], markup=False)
        if h.get("refs"):
            console.print(f"  refs: {', '.join(h['refs'])}", style="dim")
        console.print()


@held_app.command("approve")
def held_approve(
    jar: str = typer.Argument(...),
    held_id: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent", help="Who is approving this"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        mess = c.approve_held(jar, held_id, approved_by=agent)
    console.print(f"approved [cyan]{held_id}[/] → sent as [green]{mess['id']}[/] seq={mess.get('seq')}")


@held_app.command("reject")
def held_reject(
    jar: str = typer.Argument(...),
    held_id: str = typer.Argument(...),
    agent: Optional[str] = typer.Option(None, "--agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(None, "--password", "-p", envvar="MESSJAR_PASSWORD"),
) -> None:
    with _client(bus, password) as c:
        h = c.reject_held(jar, held_id, rejected_by=agent)
    console.print(f"[cyan]{held_id}[/] status={h['status']}")


@daemon_app.command("run")
def daemon_run(
    agent: str = typer.Option(..., "--agent"),
    adapter: str = typer.Option(
        ..., "--adapter", help="claude_code | cursor | codex | opencode"
    ),
    workdir: Path = typer.Option(Path.cwd(), "--workdir"),
    jar: Optional[str] = typer.Option(None, "--jar"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus", envvar="MESSJAR_BUS"),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="MESSJAR_PASSWORD"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Desktop notification when a Mess arrives"
    ),
    poll: float = typer.Option(2.0, "--poll"),
    max_hops: int = typer.Option(32, "--max-hops"),
    once: bool = typer.Option(False, "--once", help="Poll once and exit"),
) -> None:
    """Run the local daemon for one agent identity."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from messjar.daemon.runner import Daemon

    d = Daemon(
        bus_url=bus,
        agent_id=agent,
        adapter_name=adapter,
        workdir=workdir,
        jar=jar,
        dry_run=dry_run,
        poll_s=poll,
        max_hops=max_hops,
        password=password,
        notify=notify,
    )
    if once:
        n = d.tick()
        console.print(f"handled {n} mess(es)")
        return
    try:
        d.run_forever()
    except KeyboardInterrupt:
        console.print("stopped")


if __name__ == "__main__":
    app()
