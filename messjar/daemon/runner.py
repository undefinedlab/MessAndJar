"""Daemon: poll the bus, dispatch Messes through an adapter, optionally reply."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from messjar.client import BusClient
from messjar.daemon.adapters import get_adapter
from messjar.notify import notify_mess
from messjar.schema import MessKind

log = logging.getLogger("messjar.daemon")


class Daemon:
    def __init__(
        self,
        *,
        bus_url: str,
        agent_id: str,
        adapter_name: str,
        workdir: str | Path,
        jar: str | None = None,
        dry_run: bool = False,
        poll_s: float = 2.0,
        max_hops: int = 32,
        auto_reply: bool = True,
        password: str | None = None,
        notify: bool = True,
    ) -> None:
        self.bus = BusClient(bus_url, password=password)
        self.agent_id = agent_id
        self.adapter = get_adapter(adapter_name)
        self.workdir = str(Path(workdir).expanduser().resolve())
        self.jar_filter = jar
        self.dry_run = dry_run
        self.poll_s = poll_s
        self.max_hops = max_hops
        self.auto_reply = auto_reply
        self.notify = notify
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        log.info(
            "daemon start agent=%s adapter=%s workdir=%s dry_run=%s available=%s",
            self.agent_id,
            self.adapter.name,
            self.workdir,
            self.dry_run,
            self.adapter.available(),
        )
        while not self._stop:
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed")
            time.sleep(self.poll_s)

    def poll_once(self) -> int:
        batch = self.bus.check_jar(self.agent_id, self.jar_filter, ack=True)
        handled = 0
        for item in batch:
            jar = item["jar"]
            for mess in item["messes"]:
                self._handle(jar, mess)
                handled += 1
        return handled

    def _handle(self, jar: dict[str, Any], mess: dict[str, Any]) -> None:
        kind = mess.get("kind")
        hop = int(mess.get("hop") or 0)
        log.info(
            "recv jar=%s id=%s kind=%s from=%s hop=%s",
            jar.get("name"),
            mess.get("id"),
            kind,
            mess.get("from"),
            hop,
        )

        if self.notify:
            ok = notify_mess(jar, mess)
            log.info("desktop notify %s", "sent" if ok else "skipped")

        local = (jar.get("local") or {}).get(self.agent_id) or {}
        if jar.get("paused") or local.get("paused"):
            log.info("skip paused jar/agent")
            return

        if hop >= self.max_hops:
            log.warning("hop limit %s reached; not invoking", self.max_hops)
            return

        # fyi: notify already fired; queue without spawning an agent
        wakes = kind in (
            MessKind.question.value,
            MessKind.handoff.value,
            MessKind.answer.value,
        )
        if not wakes and kind == MessKind.fyi.value:
            log.info("fyi queued (no wake); recording only")
            self.bus.update_local(
                jar["id"],
                self.agent_id,
                workdir=self.workdir,
                hop=hop,
            )
            return

        session_id = local.get("session_id")
        result = self.adapter.invoke(
            mess,
            workdir=local.get("workdir") or self.workdir,
            session_id=session_id,
            dry_run=self.dry_run or not self.adapter.available(),
        )
        if result.dry_run:
            log.info("dry-run cmd=%s", result.command)
        if not result.ok:
            log.error("adapter failed: %s", result.error)
            return

        new_session = result.session_id or session_id
        self.bus.update_local(
            jar["id"],
            self.agent_id,
            workdir=self.workdir,
            session_id=new_session,
            hop=hop,
        )

        if (
            self.auto_reply
            and mess.get("reply_expected")
            and result.reply_body
            and result.reply_kind
        ):
            reply = self.bus.send(
                jar["id"],
                from_agent=self.agent_id,
                to_agent=mess["from"],
                body=result.reply_body,
                kind=result.reply_kind,
                reply_expected=False,
                hop=hop + 1,
                refs=[f"mess:{mess['id']}"],
            )
            log.info("sent reply id=%s hop=%s", reply.get("id"), hop + 1)
