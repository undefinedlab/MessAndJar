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
DEFAULT_DB = os.environ.get("MESSJAR_DB", str(Path.home() / ".messjar" / "bus.db"))


def _client(bus: str) -> BusClient:
    return BusClient(bus)


@bus_app.command("serve")
def bus_serve(
    db: str = typer.Option(DEFAULT_DB, "--db", help="SQLite path"),
    host: str = typer.Option(None, "--host", help="Bind host (default 0.0.0.0 in container)"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """Start the HTTP/MCP bus."""
    from messjar.bus.server import run_server

    bind_host = host or os.environ.get("HOST", "0.0.0.0")
    bind_port = port or int(os.environ.get("PORT", "7420"))
    db_path = os.environ.get("MESSJAR_DB", db)
    console.print(f"[bold]Mess&Jar bus[/] db={db_path} http://{bind_host}:{bind_port}")
    run_server(db_path, host=bind_host, port=bind_port)


@jars_app.command("create")
def jars_create(
    name: str = typer.Argument(..., help="Jar name"),
    agents: str = typer.Option(..., "--agents", help="Comma-separated agent ids"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
) -> None:
    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    with _client(bus) as c:
        jar = c.create_jar(name, agent_list)
    console.print(f"created [cyan]{jar['name']}[/] id={jar['id']} agents={jar['agents']}")


@jars_app.command("list")
def jars_list(
    agent: Optional[str] = typer.Option(None, "--agent"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
) -> None:
    with _client(bus) as c:
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


# alias: mj jars → list
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
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
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
    with _client(bus) as c:
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
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
) -> None:
    """Watch Messes in a Jar."""
    with _client(bus) as c:
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
    jar: str = typer.Argument(...),
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
) -> None:
    with _client(bus) as c:
        j = c.pause(jar)
    console.print(f"paused [cyan]{j['name']}[/]")


@app.command("resume")
def resume_jar(
    jar: str = typer.Argument(...),
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
) -> None:
    with _client(bus) as c:
        j = c.resume(jar)
    console.print(f"resumed [cyan]{j['name']}[/]")


@daemon_app.command("run")
def daemon_run(
    agent: str = typer.Option(..., "--agent"),
    adapter: str = typer.Option(..., "--adapter", help="claude_code | cursor"),
    workdir: Path = typer.Option(Path.cwd(), "--workdir"),
    jar: Optional[str] = typer.Option(None, "--jar"),
    bus: str = typer.Option(DEFAULT_BUS, "--bus"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    )
    if once:
        n = d.poll_once()
        console.print(f"handled {n} mess(es)")
        return
    try:
        d.run_forever()
    except KeyboardInterrupt:
        console.print("stopped")


if __name__ == "__main__":
    app()
