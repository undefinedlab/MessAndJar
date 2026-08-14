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
app.add_typer(jars_app, name="jars")
app.add_typer(bus_app, name="bus")
app.add_typer(daemon_app, name="daemon")

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
        )
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
